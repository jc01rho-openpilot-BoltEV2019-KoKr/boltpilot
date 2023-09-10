
from cereal import car
from openpilot.common.conversions import Conversions as CV
from openpilot.common.numpy_fast import interp, clip
from openpilot.common.realtime import DT_CTRL
from opendbc.can.packer import CANPacker
from openpilot.selfdrive.car import apply_driver_steer_torque_limits, create_gas_interceptor_command
from openpilot.selfdrive.car.gm import gmcan
from openpilot.selfdrive.car.gm.values import DBC, CanBus, CarControllerParams, CruiseButtons, GMFlags, CC_ONLY_CAR, EV_CAR
import math
import cereal.messaging as messaging
import numpy as np
## Boltpiot things
from collections import deque
from openpilot.common.params import Params
##
VisualAlert = car.CarControl.HUDControl.VisualAlert
NetworkLocation = car.CarParams.NetworkLocation
LongCtrlState = car.CarControl.Actuators.LongControlState
GearShifter = car.CarState.GearShifter
TransmissionType = car.CarParams.TransmissionType

# Camera cancels up to 0.1s after brake is pressed, ECM allows 0.5s
CAMERA_CANCEL_DELAY_FRAMES = 10
# Enforce a minimum interval between steering messages to avoid a fault
MIN_STEER_MSG_INTERVAL_MS = 15


def actuator_hystereses(final_pedal, pedal_steady, pedal_hyst_gap_param = 0.01):
  # hyst params... TODO: move these to VehicleParams
      # don't change pedal command for small oscillations within this value
  # pedal_hyst_gap= 0.01
  pedal_hyst_gap= pedal_hyst_gap_param
  # for small pedal oscillations within pedal_hyst_gap, don't change the pedal command
  if math.isclose(final_pedal, 0.0):
    pedal_steady = 0.
  elif final_pedal > pedal_steady + pedal_hyst_gap:
    pedal_steady = final_pedal - pedal_hyst_gap
  elif final_pedal < pedal_steady - pedal_hyst_gap:
    pedal_steady = final_pedal + pedal_hyst_gap
  final_pedal = pedal_steady

  return final_pedal, pedal_steady


class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.start_time = 0.
    self.apply_steer_last = 0
    self.apply_gas = 0
    self.apply_brake = 0
    self.apply_speed = 0
    self.frame = 0
    self.last_steer_frame = 0
    self.last_button_frame = 0
    self.cancel_counter = 0
    self.pedal_steady = 0.

    self.lka_steering_cmd_counter = 0
    self.lka_icon_status_last = (False, False)

    self.params = CarControllerParams(self.CP)

    self.packer_pt = CANPacker(DBC[self.CP.carFingerprint]['pt'])
    self.packer_obj = CANPacker(DBC[self.CP.carFingerprint]['radar'])
    self.packer_ch = CANPacker(DBC[self.CP.carFingerprint]['chassis'])

    self.pedalGas_valueStore = 0.0
    self.pedalGasRaw_valueStore = 0.0
    self.pedalGasAvg_valueStore = 0.0
    self.pedalGasBufferSize = 20
    self.pedalGasBuffer = deque(maxlen=self.pedalGasBufferSize)

    self.aEgoAvg_valueStore = 0.0
    self.aEgoBufferSize = 10
    self.aEgoBuffer = deque(maxlen=self.aEgoBufferSize)

    self.pedal_hyst_gap = 1.0
    self.pedal_gas_max = 0.3275
    # self.current_pitch_debug = 0.0
    #pitch :  캘리브레이션 설정창에 나오는 장치는 n 도 위로 보고있고.. 의 그것.
    self.current_pitch = -100.0
    self.default_pitch = -100.0


    self.sm = messaging.SubMaster(['liveCalibration'])


  def update(self, CC, CS, now_nanos):

    if self.frame % 10 == 0:
      self.sm.update(0)


    if self.sm.updated['liveCalibration']:
      # self.current_pitch_debug = self.sm['liveCalibration'].liveCalibration.liveMpcDebug.currentPitch
      if self.default_pitch == -100.0:
        self.default_pitch = np.asarray(self.sm['liveCalibration'].rpyCalib)[1]
      else :
        self.current_pitch = np.asarray(self.sm['liveCalibration'].rpyCalib)[1]

    actuators = CC.actuators
    hud_control = CC.hudControl
    hud_alert = hud_control.visualAlert
    hud_v_cruise = hud_control.setSpeed
    if hud_v_cruise > 70:
      hud_v_cruise = 0

    # Send CAN commands.
    can_sends = []

    # Steering (Active: 50Hz, inactive: 10Hz)
    steer_step = self.params.STEER_STEP if CC.latActive else self.params.INACTIVE_STEER_STEP

    if self.CP.networkLocation == NetworkLocation.fwdCamera:
      # Also send at 50Hz:
      # - on startup, first few msgs are blocked
      # - until we're in sync with camera so counters align when relay closes, preventing a fault.
      #   openpilot can subtly drift, so this is activated throughout a drive to stay synced
      out_of_sync = self.lka_steering_cmd_counter % 4 != (CS.cam_lka_steering_cmd_counter + 1) % 4
      if CS.loopback_lka_steering_cmd_ts_nanos == 0 or out_of_sync:
        steer_step = self.params.STEER_STEP

    self.lka_steering_cmd_counter += 1 if CS.loopback_lka_steering_cmd_updated else 0

    # Avoid GM EPS faults when transmitting messages too close together: skip this transmit if we
    # received the ASCMLKASteeringCmd loopback confirmation too recently
    last_lka_steer_msg_ms = (now_nanos - CS.loopback_lka_steering_cmd_ts_nanos) * 1e-6
    if (self.frame - self.last_steer_frame) >= steer_step and last_lka_steer_msg_ms > MIN_STEER_MSG_INTERVAL_MS:
      # Initialize ASCMLKASteeringCmd counter using the camera until we get a msg on the bus
      if CS.loopback_lka_steering_cmd_ts_nanos == 0:
        self.lka_steering_cmd_counter = CS.pt_lka_steering_cmd_counter + 1

      if CC.latActive:
        new_steer = int(round(actuators.steer * self.params.STEER_MAX))
        apply_steer = apply_driver_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, self.params)
      else:
        apply_steer = 0

      self.last_steer_frame = self.frame
      self.apply_steer_last = apply_steer
      idx = self.lka_steering_cmd_counter % 4
      can_sends.append(gmcan.create_steering_control(self.packer_pt, CanBus.POWERTRAIN, apply_steer, idx, CC.latActive))

    if self.CP.openpilotLongitudinalControl:
      # Gas/regen, brakes, and UI commands - all at 25Hz

      if CC.longActive and actuators.accel < -1.0:
        can_sends.append(gmcan.create_regen_paddle_command(self.packer_pt, CanBus.POWERTRAIN))
        actuators.regenPaddle = True  # for icon
      else:
        actuators.regenPaddle = False # for icon


      if not CC.longActive:
        # ASCM sends max regen when not enabled
        self.apply_gas = self.params.INACTIVE_REGEN
        self.apply_brake = 0
      elif CC.longActive and self.CP.carFingerprint in CC_ONLY_CAR and not CS.CP.enableGasInterceptor:
        # BEGIN CC-ACC ######
        # TODO: Cleanup the timing - normal is every 30ms...

        cruiseBtn = CruiseButtons.INIT
        # We will spam the up/down buttons till we reach the desired speed
        # TODO: Apparently there are rounding issues.
        speedSetPoint = int(round(CS.out.cruiseState.speed * CV.MS_TO_MPH))
        speedActuator = math.floor(actuators.speed * CV.MS_TO_MPH)
        speedDiff = (speedActuator - speedSetPoint)

        # We will spam the up/down buttons till we reach the desired speed
        rate = 0.64
        if speedDiff < 0:
          cruiseBtn = CruiseButtons.DECEL_SET
          rate = 0.2
        elif speedDiff > 0:
          cruiseBtn = CruiseButtons.RES_ACCEL

        # Check rlogs closely - our message shouldn't show up on the pt bus for us
        # Or bus 2, since we're forwarding... but I think it does
        # TODO: Cleanup the timing - normal is every 30ms...
        if (cruiseBtn != CruiseButtons.INIT) and ((self.frame - self.last_button_frame) * DT_CTRL > rate):
          self.last_button_frame = self.frame
          can_sends.append(gmcan.create_buttons(self.packer_pt, CanBus.POWERTRAIN, CS.buttons_counter, cruiseBtn))
          # END CC-ACC #######
        self.apply_gas = int(round(interp(actuators.accel, self.params.GAS_LOOKUP_BP, self.params.GAS_LOOKUP_V)))
        self.apply_brake = int(round(interp(actuators.accel, self.params.BRAKE_LOOKUP_BP, self.params.BRAKE_LOOKUP_V)))

      # BEGIN INTERCEPTOR ############################
      if CS.CP.enableGasInterceptor:
        # TODO: JJS Detect saturated battery?
        if CS.single_pedal_mode:

          self.pedal_gas_max = interp(CS.out.vEgo, [0.0, 5, 30], [0.20, 0.3275,  0.3725])

          # if actuators.accel > 0.:
          #   accGain = interp(CS.out.vEgo, [0., 5], [0.23, 0.130])
          # else:
          #   accGain = interp(CS.out.vEgo, [0., 5], [0.23, 0.165])
          accGain = 0.1429
          accGain2 = interp(actuators.accel, [-3.5, 2], [0.1667, 0.1325])
          zero = interp(CS.out.vEgo,[0., 5, 10, 30], [0, accGain2, 0.19, 0.265])
          pedal_gas = clip((actuators.accel * accGain + zero), 0.0, 1.0)

          self.pedalGasRaw_valueStore = pedal_gas

        else:
          pedal_gas = clip(actuators.accel, 0., 1.)

        if not CC.longActive:
          pedal_gas = 0.0  # May not be needed with the enable param

        # self.pedal_hyst_gap = interp(CS.out.vEgo, [40.0 * CV.KPH_TO_MS, 100.0 * CV.KPH_TO_MS], [0.01, 0.0050])
        # pedal_final, self.pedal_steady = actuator_hystereses(pedal_gas, self.pedal_steady, self.pedal_hyst_gap)
        pedal_gas = clip(pedal_gas, 0., self.pedal_gas_max)
        if self.frame % 4 == 0:
          idx = (self.frame // 4) % 4
          can_sends.append(create_gas_interceptor_command(self.packer_pt, pedal_gas, idx))
          self.pedalGas_valueStore = pedal_gas
          # END INTERCEPTOR ############################



      else:
        if self.frame % 4 == 0:
          idx = (self.frame // 4) % 4
          stopping = actuators.longControlState == LongCtrlState.stopping

          at_full_stop = CC.longActive and CS.out.standstill
          near_stop = CC.longActive and (CS.out.vEgo < self.params.NEAR_STOP_BRAKE_PHASE)
          friction_brake_bus = CanBus.CHASSIS
          # GM Camera exceptions
          # TODO: can we always check the longControlState?
          if self.CP.networkLocation == NetworkLocation.fwdCamera and self.CP.carFingerprint not in CC_ONLY_CAR:
            at_full_stop = at_full_stop and stopping
            friction_brake_bus = CanBus.POWERTRAIN

          # GasRegenCmdActive needs to be 1 to avoid cruise faults. It describes the ACC state, not actuation
          can_sends.append(gmcan.create_gas_regen_command(self.packer_pt, CanBus.POWERTRAIN, self.apply_gas, idx, CC.longActive, at_full_stop))
          can_sends.append(gmcan.create_friction_brake_command(self.packer_ch, friction_brake_bus, self.apply_brake,
                                                             idx, CC.longActive, near_stop, at_full_stop, self.CP))

        # Send dashboard UI commands (ACC status)
        send_fcw = hud_alert == VisualAlert.fcw
        #can_sends.append(gmcan.create_acc_dashboard_command(self.packer_pt, CanBus.POWERTRAIN, CC.enabled, hud_control.cruiseGap,
        #                                                    hud_v_cruise * CV.MS_TO_KPH, hud_control.leadVisible, send_fcw))
        can_sends.append(gmcan.create_acc_dashboard_command(self.packer_pt, CanBus.POWERTRAIN, CC.longActive, hud_control.cruiseGap,
                                                            hud_v_cruise * CV.MS_TO_KPH, hud_control.leadVisible, send_fcw))

      # Radar needs to know current speed and yaw rate (50hz),
      # and that ADAS is alive (10hz)
      if not self.CP.radarUnavailable:
        tt = self.frame * DT_CTRL
        time_and_headlights_step = 10
        if self.frame % time_and_headlights_step == 0:
          idx = (self.frame // time_and_headlights_step) % 4
          can_sends.append(gmcan.create_adas_time_status(CanBus.OBSTACLE, int((tt - self.start_time) * 60), idx))
          can_sends.append(gmcan.create_adas_headlights_status(self.packer_obj, CanBus.OBSTACLE))

        speed_and_accelerometer_step = 2
        if self.frame % speed_and_accelerometer_step == 0:
          idx = (self.frame // speed_and_accelerometer_step) % 4
          can_sends.append(gmcan.create_adas_steering_status(CanBus.OBSTACLE, idx))
          can_sends.append(gmcan.create_adas_accelerometer_speed_status(CanBus.OBSTACLE, CS.out.vEgo, idx))

      if self.CP.networkLocation == NetworkLocation.gateway and self.frame % self.params.ADAS_KEEPALIVE_STEP == 0:
        can_sends += gmcan.create_adas_keepalive(CanBus.POWERTRAIN)

      # TODO: integrate this with the code block below?
      if (
          (self.CP.flags & GMFlags.PEDAL_LONG.value)  # Always cancel stock CC when using pedal interceptor
          or (self.CP.flags & GMFlags.CC_LONG.value and not CC.enabled)  # Cancel stock CC if OP is not active
      ) and CS.out.cruiseState.enabled:
        if (self.frame - self.last_button_frame) * DT_CTRL > 0.04:
          self.last_button_frame = self.frame
          can_sends.append(gmcan.create_buttons(self.packer_pt, CanBus.POWERTRAIN, (CS.buttons_counter + 1) % 4, CruiseButtons.CANCEL))

    else:
      # While car is braking, cancel button causes ECM to enter a soft disable state with a fault status.
      # A delayed cancellation allows camera to cancel and avoids a fault when user depresses brake quickly
      self.cancel_counter = self.cancel_counter + 1 if CC.cruiseControl.cancel else 0

      # Stock longitudinal, integrated at camera
      if (self.frame - self.last_button_frame) * DT_CTRL > 0.04:
        if self.cancel_counter > CAMERA_CANCEL_DELAY_FRAMES:
          self.last_button_frame = self.frame
########### ORIGIN
    #       if self.CP.carFingerprint in CC_ONLY_CAR:
    #         can_sends.append(gmcan.create_buttons(self.packer_pt, CanBus.POWERTRAIN, CS.buttons_counter, CruiseButtons.CANCEL))
    #       else:
    #         can_sends.append(gmcan.create_buttons(self.packer_pt, CanBus.CAMERA, CS.buttons_counter, CruiseButtons.CANCEL))
    # if self.CP.networkLocation == NetworkLocation.fwdCamera and self.CP.carFingerprint not in CC_ONLY_CAR:
########### FIXED
          can_sends.append(gmcan.create_buttons(self.packer_pt, CanBus.CAMERA, CS.buttons_counter, CruiseButtons.CANCEL))

    if self.CP.networkLocation == NetworkLocation.fwdCamera:
########### DONE
      # Silence "Take Steering" alert sent by camera, forward PSCMStatus with HandsOffSWlDetectionStatus=1
      if self.frame % 10 == 0:
        can_sends.append(gmcan.create_pscm_status(self.packer_pt, CanBus.CAMERA, CS.pscm_status))

    # Show green icon when LKA torque is applied, and
    # alarming orange icon when approaching torque limit.
    # If not sent again, LKA icon disappears in about 5 seconds.
    # Conveniently, sending camera message periodically also works as a keepalive.
    lka_active = CS.lkas_status == 1
    lka_critical = lka_active and abs(actuators.steer) > 0.9
    lka_icon_status = (lka_active, lka_critical)

    # SW_GMLAN not yet on cam harness, no HUD alerts


    if self.CP.networkLocation != NetworkLocation.fwdCamera and \
      (self.frame % self.params.CAMERA_KEEPALIVE_STEP == 0 or lka_icon_status != self.lka_icon_status_last):

      steer_alert = hud_alert in (VisualAlert.steerRequired, VisualAlert.ldw)
      can_sends.append(gmcan.create_lka_icon_command(CanBus.SW_GMLAN, lka_active, lka_critical, steer_alert))
      self.lka_icon_status_last = lka_icon_status


    actuators.pedalGas = self.pedalGas_valueStore
    actuators.pedalGasRaw = self.pedalGasRaw_valueStore
    actuators.pedalGasAvg = self.pedalGasAvg_valueStore
    actuators.aEgoAvg = self.aEgoAvg_valueStore



    new_actuators = actuators.copy()
    new_actuators.steer = self.apply_steer_last / self.params.STEER_MAX
    new_actuators.steerOutputCan = self.apply_steer_last
    new_actuators.gas = self.apply_gas
    new_actuators.brake = self.apply_brake
    new_actuators.speed = self.apply_speed

    self.frame += 1
    return new_actuators, can_sends
