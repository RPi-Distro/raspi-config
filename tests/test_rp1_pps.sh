#!/bin/sh
# Tests for the RP1 PPS raspi-config integration.
# shellcheck disable=SC1091

id() {
  if [ "$1" = "-u" ]; then
    echo 0
  else
    command id "$@"
  fi
}

export INTERACTIVE=False
. ../raspi-config

whiptail() {
  case " $* " in
    *" --menu "*)
      printf '%s\n' "$*" >> "$TEST_DIR/whiptail.log"
      printf '%s' "${MOCK_MENU_SELECTION:-}" >&3
      return "${MOCK_MENU_STATUS:-1}"
      ;;
    *" --yesno "*)
      printf '%s\n' "$*" >> "$TEST_DIR/whiptail.log"
      return "${MOCK_YESNO_STATUS:-0}"
      ;;
    *" --msgbox "*)
      printf '%s\n' "$*" >> "$TEST_DIR/whiptail.log"
      return 0
      ;;
  esac
  return 0
}

systemctl() {
  printf '%s\n' "$*" >> "$TEST_DIR/systemctl.log"
  case "$1" in
    cat)
      [ "${MOCK_UNIT_MISSING:-0}" = 0 ]
      ;;
    daemon-reload) return 0 ;;
    is-enabled) [ "${MOCK_ENABLED:-0}" = 1 ] ;;
    is-active) [ "${MOCK_ACTIVE:-0}" = 1 ] ;;
    stop)
      [ "${MOCK_STOP_FAIL:-0}" = 0 ] || return 1
      MOCK_ACTIVE=0
      ;;
    start)
      MOCK_ACTIVE=1
      ;;
    enable)
      if [ "${2:-}" = "--now" ]; then
        [ "${MOCK_ENABLE_NOW_FAIL:-0}" = 0 ] || return 1
        MOCK_ENABLED=1
        MOCK_ACTIVE=1
      else
        MOCK_ENABLED=1
      fi
      ;;
    disable)
      if [ "${2:-}" = "--now" ]; then
        MOCK_ACTIVE=0
      fi
      MOCK_ENABLED=0
      ;;
    *) return 1 ;;
  esac
}

setUp() {
  TEST_DIR=$(mktemp -d)
  RP1_PPS_CONFIG="$TEST_DIR/rp1-pps"
  RP1_PPS_SERVICE=rp1-pps.service
  RP1_PPS_TOOL="$TEST_DIR/bin-rp1-pps"
  export TEST_DIR RP1_PPS_CONFIG RP1_PPS_SERVICE RP1_PPS_TOOL
  : > "$TEST_DIR/systemctl.log"
  : > "$TEST_DIR/whiptail.log"
  cat > "$RP1_PPS_TOOL" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$TEST_DIR/tool.log"
[ "${MOCK_PLAN_FAIL:-0}" = 0 ]
EOF
  chmod 0755 "$RP1_PPS_TOOL"
  MOCK_ENABLED=0
  MOCK_ACTIVE=0
  MOCK_PLAN_FAIL=0
  MOCK_ENABLE_NOW_FAIL=0
  MOCK_STOP_FAIL=0
  MOCK_UNIT_MISSING=0
  MOCK_MENU_STATUS=1
  MOCK_YESNO_STATUS=0
  export MOCK_ENABLED MOCK_ACTIVE MOCK_PLAN_FAIL MOCK_ENABLE_NOW_FAIL
  export MOCK_STOP_FAIL MOCK_UNIT_MISSING MOCK_MENU_STATUS MOCK_YESNO_STATUS
}

tearDown() {
  rm -rf "$TEST_DIR"
}

test_menu_shows_pps_only_on_pi5_class_boards() {
  is_pi() { return 0; }
  is_pifive() { return 0; }
  do_interface_menu
  assertTrue "Pi 5 menu should include RP1 PPS" "grep -q 'I8 RP1 PTP PPS' '$TEST_DIR/whiptail.log'"

  : > "$TEST_DIR/whiptail.log"
  is_pifive() { return 1; }
  do_interface_menu
  assertFalse "other boards should not include RP1 PPS" "grep -q 'I8 RP1 PTP PPS' '$TEST_DIR/whiptail.log'"
}

test_gpio_selector_includes_all_bcm_pins_and_physical_pin_mapping() {
  MOCK_MENU_STATUS=0
  MOCK_MENU_SELECTION=18
  export MOCK_MENU_STATUS MOCK_MENU_SELECTION
  selected=$(rp1_pps_select_gpio)
  assertEquals 0 "$?"
  assertEquals 18 "$selected"
  assertTrue "GPIO0 HAT ID warning should be visible" "grep -q 'GPIO0' '$TEST_DIR/whiptail.log'"
  assertTrue "GPIO27 physical pin mapping should be visible" "grep -q 'BCM GPIO 27 (physical pin 13)' '$TEST_DIR/whiptail.log'"
}

test_configure_output_writes_atomic_service_config_and_enables_service() {
  rp1_pps_configure output 23 rising 100000000
  assertEquals 0 "$?"
  assertTrue "output mode should be configured" "grep -q '^PPS_MODE=output$' '$RP1_PPS_CONFIG'"
  assertTrue "selected GPIO should be configured" "grep -q '^PPS_GPIO=23$' '$RP1_PPS_CONFIG'"
  assertTrue "selected output width should be configured" "grep -q '^PPS_HIGH_NS=100000000$' '$RP1_PPS_CONFIG'"
  assertEquals 1 "$MOCK_ENABLED"
  assertEquals 1 "$MOCK_ACTIVE"
  assertTrue "preflight should be plan-only" "grep -q -- '--mode output --gpio 23 --edge rising' '$TEST_DIR/tool.log'"
}

test_invalid_gpio_is_rejected_without_touching_service() {
  rp1_pps_configure output 28 rising
  assertNotEquals 0 "$?"
  assertFalse "invalid GPIO should not write a config" "[ -e '$RP1_PPS_CONFIG' ]"
  assertFalse "invalid GPIO should not touch systemd" "[ -s '$TEST_DIR/systemctl.log' ]"
}

test_invalid_output_width_is_rejected_before_preflight() {
  rp1_pps_configure output 23 rising 42
  assertNotEquals 0 "$?"
  assertFalse "invalid width should not write a config" "[ -e '$RP1_PPS_CONFIG' ]"
  assertFalse "invalid width should not touch systemd" "[ -s '$TEST_DIR/systemctl.log' ]"
}

test_missing_runtime_package_is_reported_without_systemd_changes() {
  rm -f "$RP1_PPS_TOOL"
  rp1_pps_configure input 18 rising
  assertNotEquals 0 "$?"
  assertTrue "missing package should be explained" "grep -q 'rp1-ptp-pps package' '$TEST_DIR/whiptail.log'"
  assertFalse "missing package should not touch systemd" "[ -s '$TEST_DIR/systemctl.log' ]"
}

test_missing_service_unit_is_reported_without_configuration() {
  MOCK_UNIT_MISSING=1
  export MOCK_UNIT_MISSING
  rp1_pps_configure input 18 rising
  assertNotEquals 0 "$?"
  assertFalse "missing service should not write config" "[ -e '$RP1_PPS_CONFIG' ]"
  assertTrue "missing unit should be explained" "grep -q 'systemd service is not installed' '$TEST_DIR/whiptail.log'"
}

test_unavailable_phc_does_not_start_a_new_service() {
  MOCK_PLAN_FAIL=1
  export MOCK_PLAN_FAIL
  rp1_pps_configure input 18 falling
  assertNotEquals 0 "$?"
  assertFalse "failed PHC validation should not write config" "[ -e '$RP1_PPS_CONFIG' ]"
  assertEquals 0 "$MOCK_ENABLED"
  assertEquals 0 "$MOCK_ACTIVE"
}

test_successful_direction_switch_replaces_config_and_restarts_service() {
  printf 'PPS_MODE=input\nPPS_GPIO=18\n' > "$RP1_PPS_CONFIG"
  MOCK_ENABLED=1
  MOCK_ACTIVE=1
  export MOCK_ENABLED MOCK_ACTIVE
  rp1_pps_configure output 23 rising 1000000
  assertEquals 0 "$?"
  assertTrue "switch should select output" "grep -q '^PPS_MODE=output$' '$RP1_PPS_CONFIG'"
  assertTrue "switch should update GPIO" "grep -q '^PPS_GPIO=23$' '$RP1_PPS_CONFIG'"
  assertTrue "switch should update pulse width" "grep -q '^PPS_HIGH_NS=1000000$' '$RP1_PPS_CONFIG'"
  assertEquals 1 "$MOCK_ENABLED"
  assertEquals 1 "$MOCK_ACTIVE"
}

test_menu_cancel_makes_no_configuration_or_service_changes() {
  MOCK_MENU_STATUS=1
  export MOCK_MENU_STATUS
  do_rp1_pps
  assertEquals 0 "$?"
  assertFalse "cancel should not create config" "[ -e '$RP1_PPS_CONFIG' ]"
  assertFalse "cancel should not touch systemd" "[ -s '$TEST_DIR/systemctl.log' ]"
}

test_failed_preflight_preserves_old_configuration_and_service() {
  printf 'PPS_MODE=input\nPPS_GPIO=18\n' > "$RP1_PPS_CONFIG"
  MOCK_ENABLED=1
  MOCK_ACTIVE=1
  MOCK_PLAN_FAIL=1
  export MOCK_ENABLED MOCK_ACTIVE MOCK_PLAN_FAIL
  rp1_pps_configure output 23 rising
  assertNotEquals 0 "$?"
  assertEquals "PPS_MODE=input
PPS_GPIO=18" "$(cat "$RP1_PPS_CONFIG")"
  assertEquals 1 "$MOCK_ENABLED"
  assertEquals 1 "$MOCK_ACTIVE"
}

test_activation_failure_restores_old_configuration_and_service_state() {
  printf 'PPS_MODE=input\nPPS_GPIO=18\n' > "$RP1_PPS_CONFIG"
  MOCK_ENABLED=1
  MOCK_ACTIVE=1
  MOCK_ENABLE_NOW_FAIL=1
  export MOCK_ENABLED MOCK_ACTIVE MOCK_ENABLE_NOW_FAIL
  rp1_pps_configure output 23 rising
  assertNotEquals 0 "$?"
  assertEquals "PPS_MODE=input
PPS_GPIO=18" "$(cat "$RP1_PPS_CONFIG")"
  assertEquals 1 "$MOCK_ENABLED"
  assertEquals 1 "$MOCK_ACTIVE"
}

test_disable_stops_service_and_removes_config() {
  printf 'PPS_MODE=output\nPPS_GPIO=23\n' > "$RP1_PPS_CONFIG"
  MOCK_ENABLED=1
  MOCK_ACTIVE=1
  export MOCK_ENABLED MOCK_ACTIVE
  rp1_pps_disable
  assertEquals 0 "$?"
  assertFalse "disabled PPS should remove its config" "[ -e '$RP1_PPS_CONFIG' ]"
  assertEquals 0 "$MOCK_ENABLED"
  assertEquals 0 "$MOCK_ACTIVE"
}

. shunit2
