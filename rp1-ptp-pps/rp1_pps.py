#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Guarded single-direction RP1 PPS control using the standard testptp CLI.

The default action is plan-only. Use --execute to make a pin mapping or start
PPS. GPIO values are RP1/BCM numbers 0..27, not physical header pin numbers.
Input and output are exclusive on the current backend.
"""
import argparse
import json
from pathlib import Path
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

from phc_identity import inventory, select_mac


CAPABILITY_PATTERNS = {
    'extts': re.compile(r'^\s*(\d+) external time stamp channels$'),
    'perout': re.compile(r'^\s*(\d+) programmable periodic signals$'),
    'pins': re.compile(r'^\s*(\d+) programmable pins$'),
}
PIN_PATTERN = re.compile(r'^name (.+) index (\d+) func (\d+) chan (\d+)$')
EVENT_PATTERN = re.compile(r'^event index (\d+) at (\d+)\.(\d{9})$')
ERROR_PREFIXES = ('opening ', 'clock_adjtime:', 'PTP_PIN_SETFUNC:',
                  'PTP_PIN_GETFUNC:',
                  'PTP_EXTTS_REQUEST', 'PTP_PEROUT_REQUEST:', 'read:')


class RP1PPS:
    def __init__(self, *, interface='eth0', testptp='testptp',
                 inventory_fn=None, monotonic=None, sleep=None,
                 popen_factory=None, output_monitor_interval=1.0,
                 input_retry_delay=1.0):
        self.interface = interface
        self.testptp = str(testptp)
        if output_monitor_interval <= 0:
            raise ValueError('output monitor interval must be positive')
        if input_retry_delay <= 0:
            raise ValueError('input retry delay must be positive')
        self.inventory_fn = inventory_fn or inventory
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.popen_factory = popen_factory or subprocess.Popen
        self.output_monitor_interval = output_monitor_interval
        self.input_retry_delay = input_retry_delay
        self.clock = None
        self.capabilities = None
        self.pins = None

    def _command(self, *options, timeout=10, acknowledgments=()):
        if self.clock is None:
            raise RuntimeError('Discover the RP1 PHC before running testptp')
        argv = [self.testptp, '-d', self.clock['device'], '-i', '0',
                *map(str, options)]
        child = self.popen_factory(argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                stdout, stderr = child.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate()
            raise TimeoutError(f'testptp timed out: {shlex.join(argv)}\n{stdout}{stderr}')
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise
        output = stdout + stderr
        if child.returncode:
            raise RuntimeError(f'testptp exited {child.returncode}: {output.strip()}')
        if any(line.startswith(ERROR_PREFIXES) for line in output.splitlines()):
            raise RuntimeError(f'testptp reported an ioctl/read error: {output.strip()}')
        for expected in acknowledgments:
            if expected not in stdout.splitlines():
                raise RuntimeError(f'testptp did not acknowledge {expected!r}: {output.strip()}')
        return stdout

    def preflight(self):
        info = self.inventory_fn(self.interface)
        self.clock = select_mac(info)
        if info['driver'] not in ('macb', 'macb_pps'):
            raise RuntimeError(f"Unexpected RP1 MAC PHC driver: {info['driver']!r}")
        caps = self.clock.get('capabilities', {})
        expected = {'extts': 1, 'perout': 1, 'pins': 28}
        if caps != expected:
            raise RuntimeError(f'Unexpected RP1 PPS capabilities: {caps!r}')
        cap_output = self._command('-c')
        observed = {}
        for line in cap_output.splitlines():
            for key, pattern in CAPABILITY_PATTERNS.items():
                match = pattern.match(line)
                if match:
                    observed[key] = int(match.group(1))
        if observed != expected:
            raise RuntimeError(f'testptp capabilities differ from sysfs: {observed!r}')
        pin_output = self._command('-l')
        pins = {}
        for line in pin_output.splitlines():
            match = PIN_PATTERN.match(line)
            if match:
                name, index, function, channel = match.groups()
                pins[int(index)] = {'name': name, 'function': int(function),
                                    'channel': int(channel)}
        if set(pins) != set(range(28)):
            raise RuntimeError(f'testptp returned an incomplete pin map: {sorted(pins)}')
        occupied = {gpio: row for gpio, row in pins.items() if row['function'] != 0}
        if occupied:
            raise RuntimeError(f'Existing PTP pin mappings must be cleared first: {occupied}')
        self.capabilities, self.pins = observed, pins
        return {'interface': self.interface, 'driver': info['driver'],
                'phc': self.clock['device'], 'capabilities': observed,
                'pins': pins}

    def plan(self, mode, gpio, *, edge='rising', events=4,
             period_ns=1_000_000_000, high_ns=10_000_000,
             phase_ns=0, duration=None, timeout=None,
             event_timeout=3.0):
        if mode not in ('input', 'output'):
            raise ValueError('mode must be input or output')
        if not isinstance(gpio, int) or not 0 <= gpio < 28:
            raise ValueError('GPIO must be an RP1/BCM header GPIO in 0..27')
        if self.pins is None:
            raise RuntimeError('Run preflight before creating a plan')
        function = 1 if mode == 'input' else 2
        base = [self.testptp, '-d', self.clock['device'], '-i', '0']
        mapping = ['-L', f'{gpio},{function}']
        if mode == 'input':
            if edge not in ('rising', 'falling'):
                raise ValueError('edge must be rising or falling')
            if events < 0:
                raise ValueError('events must be zero for continuous capture or positive')
            testptp_events = 2147483647 if events == 0 else events
            capture = ['-E', '1' if edge == 'rising' else '2',
                       '-e', str(testptp_events)]
            return {'mode': mode, 'gpio': gpio, 'edge': edge,
                    'events': events, 'continuous': events == 0,
                    'mapping': shlex.join(base + mapping),
                    'capture': shlex.join(base + mapping + capture),
                    'cleanup': shlex.join(base + ['-L', f'{gpio},0']),
                    'timeout_s': (None if events == 0 else
                                  timeout if timeout is not None else max(5, events * 3)),
                    'event_timeout_s': event_timeout if events == 0 else None,
                    'retry_delay_s': self.input_retry_delay if events == 0 else None}
        if period_ns < 1 or high_ns < 1 or high_ns >= period_ns:
            raise ValueError('require 0 < high-ns < period-ns')
        if phase_ns < 0 or phase_ns >= period_ns:
            raise ValueError('phase-ns must be in [0, period-ns)')
        start = ['-p', str(period_ns), '-H', str(phase_ns), '-w', str(high_ns)]
        return {'mode': mode, 'gpio': gpio, 'period_ns': period_ns,
                'high_ns': high_ns, 'phase_ns': phase_ns,
                'mapping': shlex.join(base + mapping),
                'start': shlex.join(base + mapping + start),
                'stop': shlex.join(base + ['-p', '0']),
                'cleanup': shlex.join(base + ['-L', f'{gpio},0']),
                'duration_s': duration}

    def _capture_continuous(self, edge, *, event_timeout, event_callback=None):
        argv = [self.testptp, '-d', self.clock['device'], '-i', '0',
                '-E', '1' if edge == 'rising' else '2', '-e', '2147483647']
        child = self.popen_factory(argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   bufsize=1)
        lines = queue.Queue()

        def read_lines():
            try:
                for line in child.stdout:
                    lines.put(line.rstrip('\r\n'))
            finally:
                lines.put(None)

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        events_received = 0
        acknowledged = False
        last_event = time.monotonic()
        stopped_by = 'process-exit'
        try:
            while True:
                remaining = event_timeout - (time.monotonic() - last_event)
                if remaining <= 0:
                    raise TimeoutError(f'No PPS input event received for {event_timeout:g} seconds')
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError(f'No PPS input event received for {event_timeout:g} seconds')
                if line is None:
                    child.wait()
                    raise RuntimeError('testptp stopped while continuous input capture was active')
                if line.startswith(ERROR_PREFIXES):
                    raise RuntimeError(f'testptp reported an input error: {line}')
                if line == 'external time stamp request okay':
                    acknowledged = True
                    continue
                match = EVENT_PATTERN.match(line)
                if not match:
                    continue
                if not acknowledged:
                    raise RuntimeError('testptp returned an event without acknowledging EXTS setup')
                channel, sec, nsec = match.groups()
                if int(channel) != 0:
                    raise RuntimeError(f'Unexpected PTP timestamp channel {channel}')
                event = {'channel': int(channel),
                         'timestamp_ns': int(sec) * 1_000_000_000 + int(nsec)}
                events_received += 1
                last_event = time.monotonic()
                if event_callback:
                    event_callback(event)
        except KeyboardInterrupt:
            stopped_by = 'signal'
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            reader.join(timeout=2)
            if child.stdout:
                child.stdout.close()
        return {'events_received': events_received, 'stopped_by': stopped_by}

    def capture(self, gpio, *, edge='rising', events=4, timeout=None,
                event_timeout=3.0, event_callback=None):
        plan = self.plan('input', gpio, edge=edge, events=events,
                         timeout=timeout, event_timeout=event_timeout)
        timeout = plan['timeout_s']
        mapped = False
        try:
            self._command('-L', f'{gpio},1', timeout=10,
                          acknowledgments=('set pin function okay',))
            mapped = True
            if events == 0:
                events_received = 0
                input_inactive = False

                def report_event(event):
                    nonlocal events_received, input_inactive
                    events_received += 1
                    if input_inactive:
                        print('PPS input resumed', file=sys.stderr, flush=True)
                        input_inactive = False
                    if event_callback:
                        event_callback(event)

                while True:
                    try:
                        capture = self._capture_continuous(edge,
                            event_timeout=event_timeout,
                            event_callback=report_event)
                    except TimeoutError as exc:
                        if not input_inactive:
                            print(f'{exc}; rearming input capture in '
                                  f'{self.input_retry_delay:g} seconds',
                                  file=sys.stderr, flush=True)
                            input_inactive = True
                        try:
                            self.sleep(self.input_retry_delay)
                        except KeyboardInterrupt:
                            return {'passed': True, 'mode': 'input',
                                    'gpio': gpio, 'edge': edge,
                                    'continuous': True,
                                    'events_received': events_received,
                                    'stopped_by': 'signal'}
                        continue
                    return {'passed': True, 'mode': 'input', 'gpio': gpio,
                            'edge': edge, 'continuous': True,
                            'events_received': events_received,
                            'stopped_by': capture['stopped_by']}
            output = self._command('-E', '1' if edge == 'rising' else '2',
                '-e', str(events),
                timeout=timeout,
                acknowledgments=('external time stamp request okay',))
            records = []
            for line in output.splitlines():
                match = EVENT_PATTERN.match(line)
                if match:
                    channel, sec, nsec = match.groups()
                    records.append({'channel': int(channel),
                                    'timestamp_ns': int(sec) * 1_000_000_000 + int(nsec)})
            if len(records) != events or any(row['channel'] != 0 for row in records):
                raise RuntimeError(f'Expected {events} channel-0 timestamps, received {records!r}')
            return {'passed': True, 'mode': 'input', 'gpio': gpio,
                    'edge': edge, 'events': records, 'testptp_output': output}
        finally:
            if mapped:
                self._command('-L', f'{gpio},0', timeout=10,
                              acknowledgments=('set pin function okay',))

    def _verify_output_mapping(self, gpio):
        output = self._command('-l', timeout=10)
        pins = {}
        for line in output.splitlines():
            match = PIN_PATTERN.match(line)
            if match:
                name, index, function, channel = match.groups()
                pins[int(index)] = {'name': name, 'function': int(function),
                                    'channel': int(channel)}
        if set(pins) != set(range(28)):
            raise RuntimeError('PTP pin map became incomplete while PPS output was active')
        occupied = {index: row for index, row in pins.items()
                    if row['function'] and index != gpio}
        if occupied:
            raise RuntimeError(f'Other PTP pin mappings appeared during output: {occupied}')
        if pins[gpio]['function'] != 2 or pins[gpio]['channel'] != 0:
            raise RuntimeError(f'GPIO{gpio} lost its PEROUT mapping while output was active')

    def output(self, gpio, *, period_ns=1_000_000_000,
               high_ns=10_000_000, phase_ns=0, duration=None):
        plan = self.plan('output', gpio, period_ns=period_ns,
                         high_ns=high_ns, phase_ns=phase_ns, duration=duration)
        mapped = False
        start_attempted = False
        cleanup_error = None
        started_at = None
        stopped_by = 'duration'
        try:
            self._command('-L', f'{gpio},2', timeout=10,
                          acknowledgments=('set pin function okay',))
            mapped = True
            start_attempted = True
            output = self._command('-p', str(period_ns),
                '-H', str(phase_ns), '-w', str(high_ns), timeout=10,
                acknowledgments=('periodic output request okay',))
            started_at = self.monotonic()
            deadline = None if duration is None else started_at + duration
            next_monitor = started_at
            try:
                while deadline is None or self.monotonic() < deadline:
                    now = self.monotonic()
                    if now >= next_monitor:
                        self._verify_output_mapping(gpio)
                        next_monitor = now + self.output_monitor_interval
                    wait = min(0.25, max(0, next_monitor - self.monotonic()))
                    if deadline is not None:
                        wait = min(wait, max(0, deadline - self.monotonic()))
                    self.sleep(wait)
            except KeyboardInterrupt:
                stopped_by = 'interrupt'
        finally:
            if start_attempted:
                try:
                    self._command('-p', '0', timeout=10,
                                  acknowledgments=('periodic output request okay',))
                except Exception as exc:
                    cleanup_error = f'PEROUT disable failed: {exc}'
            if mapped and cleanup_error is None:
                try:
                    self._command('-L', f'{gpio},0', timeout=10,
                                  acknowledgments=('set pin function okay',))
                except Exception as exc:
                    cleanup_error = f'PEROUT stopped but pin unmap failed: {exc}'
        if cleanup_error:
            raise RuntimeError(cleanup_error)
        return {'passed': True, 'mode': 'output', 'gpio': gpio,
                'period_ns': period_ns, 'high_ns': high_ns,
                'phase_ns': phase_ns, 'duration_s': duration,
                'stopped_by': stopped_by, 'testptp_output': output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='eth0')
    parser.add_argument('--testptp', default='testptp')
    parser.add_argument('--mode', choices=('input', 'output'), required=True)
    parser.add_argument('--gpio', type=int, required=True,
                        help='RP1/BCM GPIO number 0..27, not physical pin number')
    parser.add_argument('--edge', choices=('rising', 'falling'), default='rising')
    parser.add_argument('--events', type=int, default=4,
                        help='input event count; use 0 for continuous service capture')
    parser.add_argument('--timeout', type=float,
                        help='input capture timeout, default is max(5 s, 3 s/event)')
    parser.add_argument('--event-timeout', type=float, default=5.0,
                        help='seconds without a pulse before continuous input is re-armed')
    parser.add_argument('--period-ns', type=int, default=1_000_000_000)
    parser.add_argument('--high-ns', type=int, default=10_000_000)
    parser.add_argument('--phase-ns', type=int, default=0)
    parser.add_argument('--duration', type=float,
                        help='output run time; omit to run until Ctrl-C')
    parser.add_argument('--execute', action='store_true',
                        help='apply the plan; without this flag only inspect and print')
    parser.add_argument('--wait-for-phc', action='store_true',
                        help='wait for a supported RP1 PHC before starting')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        if args.timeout is not None and args.timeout <= 0:
            raise ValueError('timeout must be positive')
        if args.event_timeout <= 0:
            raise ValueError('event-timeout must be positive')
        if args.duration is not None and args.duration <= 0:
            raise ValueError('duration must be positive')
        pps = RP1PPS(interface=args.interface, testptp=args.testptp)
        while True:
            try:
                preflight = pps.preflight()
                break
            except (OSError, RuntimeError, ValueError) as exc:
                if not args.wait_for_phc:
                    raise
                print(f'Waiting for a supported RP1 PHC: {exc}', file=sys.stderr,
                      flush=True)
                time.sleep(5)
        plan = pps.plan(args.mode, args.gpio, edge=args.edge,
                        events=args.events, period_ns=args.period_ns,
                        high_ns=args.high_ns, phase_ns=args.phase_ns,
                        duration=args.duration, timeout=args.timeout,
                        event_timeout=args.event_timeout)
        result = {'preflight': preflight, 'plan': plan}
        if args.execute:
            result['result'] = (pps.capture(args.gpio, edge=args.edge,
                events=args.events, timeout=args.timeout,
                event_timeout=args.event_timeout,
                event_callback=lambda event: print(
                    json.dumps({'event': event}), flush=True)) if args.mode == 'input'
                else pps.output(args.gpio, period_ns=args.period_ns,
                    high_ns=args.high_ns, phase_ns=args.phase_ns,
                    duration=args.duration))
        print(json.dumps(result, indent=2))
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')
    except KeyboardInterrupt:
        parser.exit(130, 'interrupted\n')


def _handle_sigterm(_signum, _frame):
    """Turn service stop into an exception so active PPS is cleaned up."""
    raise KeyboardInterrupt


if __name__ == '__main__':
    main()
