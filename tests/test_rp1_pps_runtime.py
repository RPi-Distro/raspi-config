#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline tests for guarded testptp GPIO control."""
import importlib.util
import threading
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1] / 'rp1-ptp-pps'
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location('rp1_pps', ROOT / 'rp1_pps.py')
rp1 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rp1)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakePopen:
    def __init__(self, argv, output='', *, timeout=False, returncode=0):
        self.argv = argv
        self.output = output
        self.timeout = timeout
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        if self.timeout and not self.terminated:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.output, ''

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.terminated = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakeStream:
    def __init__(self, events):
        self.events = events
        self.stopped = threading.Event()

    def __iter__(self):
        yield 'external time stamp request okay\n'
        for index in range(self.events):
            yield f'event index 0 at {100 + index}.000000123\n'
        self.stopped.wait()

    def close(self):
        self.stopped.set()


class FakeStreamPopen:
    def __init__(self, argv, events):
        self.argv = argv
        self.stdout = FakeStream(events)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.stopped.set()

    def kill(self):
        self.returncode = -9
        self.stdout.stopped.set()

    def wait(self, timeout=None):
        return self.returncode


class RP1PPSTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.pin_functions = {gpio: 0 for gpio in range(28)}
        self.fail_output_stop = False
        self.capture_error = None
        self.capture_timeout = False
        self.lose_mapping_gpio = None
        self.interrupt_on_sleep = False
        self.clock = FakeClock()

        def sleep(seconds):
            self.clock.sleep(seconds)
            if self.lose_mapping_gpio is not None:
                self.pin_functions[self.lose_mapping_gpio] = 0
                self.lose_mapping_gpio = None
            if self.interrupt_on_sleep:
                self.interrupt_on_sleep = False
                raise KeyboardInterrupt

        def inventory(_interface):
            return {'driver': 'macb', 'clocks': [{
                'name': 'gem-ptp-timer', 'device': '/dev/ptp7',
                'parent': '/sys/devices/platform/rp1/net/eth0',
                'device_number': [248, 7], 'rp1_mac': True,
                'capabilities': {'extts': 1, 'perout': 1, 'pins': 28}}]}

        def popen(argv, **_kwargs):
            self.calls.append(list(argv))
            options = argv[5:]
            if options == ['-c']:
                output = ('capabilities:\n  1 external time stamp channels\n'
                          '  1 programmable periodic signals\n  28 programmable pins\n')
            elif options == ['-l']:
                output = ''.join(f'name GPIO{gpio} index {gpio} func '
                    f'{self.pin_functions[gpio]} chan 0\n' for gpio in range(28))
            elif options and options[0] == '-L':
                gpio, function = map(int, options[1].split(','))
                if function:
                    self.pin_functions[gpio] = function
                else:
                    self.pin_functions[gpio] = 0
                output = 'set pin function okay\n'
            elif options and options[0] == '-E':
                output = self.capture_error or 'external time stamp request okay\n'
                if not self.capture_error:
                    count = int(options[options.index('-e') + 1])
                    output += ''.join(f'event index 0 at {1+i}.000000123\n'
                                      for i in range(count))
            elif options and options[0] == '-p':
                if options[1] == '0' and self.fail_output_stop:
                    output = 'PTP_PEROUT_REQUEST: Connection timed out\n'
                else:
                    output = 'periodic output request okay\n'
            else:
                raise AssertionError(f'Unexpected testptp options: {options!r}')
            return FakePopen(argv, output, timeout=(options and options[0] == '-E'
                and self.capture_timeout))

        self.tool = rp1.RP1PPS(interface='eth0', testptp='/opt/testptp',
            inventory_fn=inventory, monotonic=self.clock.monotonic,
            sleep=sleep, popen_factory=popen)

    def test_capture_uses_selected_gpio_and_unmaps_after_success(self):
        self.tool.preflight()
        result = self.tool.capture(12, edge='falling', events=3)
        self.assertTrue(result['passed'])
        self.assertEqual([r['timestamp_ns'] for r in result['events']], [
            1_000_000_123, 2_000_000_123, 3_000_000_123])
        self.assertIn(['-L', '12,1'], [call[5:7] for call in self.calls])
        self.assertIn(['-E', '2', '-e', '3'], [call[5:9] for call in self.calls])
        self.assertEqual(self.pin_functions[12], 0)

    def test_capture_error_cleans_selected_mapping(self):
        self.tool.preflight()
        self.capture_error = 'external time stamp request okay\nread: Input/output error\n'
        with self.assertRaisesRegex(RuntimeError, 'ioctl/read error'):
            self.tool.capture(18)
        self.assertEqual(self.pin_functions[18], 0)
        self.assertEqual(self.calls[-1][5:7], ['-L', '18,0'])

    def test_capture_timeout_terminates_testptp_then_unmaps(self):
        self.tool.preflight()
        self.capture_timeout = True
        with self.assertRaises(TimeoutError):
            self.tool.capture(23, timeout=1)
        self.assertEqual(self.pin_functions[23], 0)
        self.assertEqual(self.calls[-1][5:7], ['-L', '23,0'])

    def test_continuous_input_streams_events_and_stops_cleanly(self):
        self.tool.preflight()
        children = []

        def popen(argv, **kwargs):
            self.calls.append(list(argv))
            if argv[-2:] == ['-e', '2147483647']:
                child = FakeStreamPopen(argv, events=2)
                children.append(child)
                return child
            return self.tool_popen(argv, **kwargs)

        self.tool_popen = self.tool.popen_factory
        self.tool.popen_factory = popen
        observed = []

        def stop_after_two(event):
            observed.append(event)
            if len(observed) == 2:
                raise KeyboardInterrupt

        result = self.tool.capture(10, events=0, event_timeout=1,
                                   event_callback=stop_after_two)
        self.assertEqual(result['events_received'], 2)
        self.assertEqual(result['stopped_by'], 'signal')
        self.assertEqual([row['timestamp_ns'] for row in observed], [
            100_000_000_123, 101_000_000_123])
        self.assertEqual(children[0].returncode, -15)
        self.assertEqual(self.pin_functions[10], 0)
        self.assertIn(['-E', '1', '-e', '2147483647'],
                      [call[5:] for call in self.calls])

    def test_continuous_input_timeout_unmaps_selected_gpio(self):
        self.tool.preflight()

        def popen(argv, **kwargs):
            self.calls.append(list(argv))
            if argv[-2:] == ['-e', '2147483647']:
                return FakeStreamPopen(argv, events=0)
            return self.tool_popen(argv, **kwargs)

        self.tool_popen = self.tool.popen_factory
        self.tool.popen_factory = popen
        with self.assertRaisesRegex(TimeoutError, 'No PPS input event'):
            self.tool.capture(11, events=0, event_timeout=0.05)
        self.assertEqual(self.pin_functions[11], 0)
        self.assertEqual(self.calls[-1][5:7], ['-L', '11,0'])

    def test_output_runs_then_disables_before_unmapping(self):
        self.tool.preflight()
        result = self.tool.output(16, period_ns=2_000_000_000,
                                  high_ns=20_000_000, phase_ns=10,
                                  duration=0.5)
        self.assertTrue(result['passed'])
        options = [call[5:] for call in self.calls]
        self.assertIn(['-L', '16,2'], options)
        self.assertIn(['-p', '2000000000', '-H', '10', '-w', '20000000'], options)
        self.assertLess(options.index(['-p', '0']), options.index(['-L', '16,0']))
        self.assertEqual(self.pin_functions[16], 0)

    def test_output_stop_failure_retains_pin_mapping(self):
        self.tool.preflight()
        self.fail_output_stop = True
        with self.assertRaisesRegex(RuntimeError, 'PEROUT disable failed'):
            self.tool.output(6, duration=0.1)
        self.assertEqual(self.pin_functions[6], 2)
        self.assertNotIn(['-L', '6,0'], [call[5:7] for call in self.calls])

    def test_output_aborts_and_cleans_up_if_mapping_disappears(self):
        self.tool.preflight()
        self.lose_mapping_gpio = 6
        with self.assertRaisesRegex(RuntimeError, 'lost its PEROUT mapping'):
            self.tool.output(6, duration=5)
        options = [call[5:] for call in self.calls]
        self.assertIn(['-l'], options)
        self.assertLess(options.index(['-p', '0']), options.index(['-L', '6,0']))
        self.assertEqual(self.pin_functions[6], 0)

    def test_interrupt_stops_output_and_runs_cleanup(self):
        self.tool.preflight()
        self.interrupt_on_sleep = True
        result = self.tool.output(6, duration=10)
        self.assertEqual(result['stopped_by'], 'interrupt')
        options = [call[5:] for call in self.calls]
        self.assertLess(options.index(['-p', '0']), options.index(['-L', '6,0']))
        self.assertEqual(self.pin_functions[6], 0)

    def test_sigterm_handler_requests_graceful_unwind(self):
        with self.assertRaises(KeyboardInterrupt):
            rp1._handle_sigterm(15, None)

    def test_preflight_refuses_preexisting_mapping_before_any_write(self):
        self.pin_functions[7] = 1
        with self.assertRaisesRegex(RuntimeError, 'Existing PTP pin mappings'):
            self.tool.preflight()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.pin_functions[7], 1)

    def test_plan_validates_gpio_and_timing_without_actuation(self):
        self.tool.preflight()
        with self.assertRaises(ValueError):
            self.tool.plan('input', 28)
        with self.assertRaises(ValueError):
            self.tool.plan('output', 18, period_ns=100, high_ns=100)
        plan = self.tool.plan('output', 23)
        self.assertIn('-L 23,2', plan['start'])
        self.assertIn('-p 0', plan['stop'])

    def test_plan_uses_testptp_long_count_for_continuous_input(self):
        self.tool.preflight()
        plan = self.tool.plan('input', 18, events=0)
        self.assertTrue(plan['continuous'])
        self.assertIn('-e 2147483647', plan['capture'])
        with self.assertRaisesRegex(ValueError, 'events must be zero'):
            self.tool.plan('input', 18, events=-1)


if __name__ == '__main__':
    unittest.main()
