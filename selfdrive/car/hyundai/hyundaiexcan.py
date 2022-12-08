import crcmod

from selfdrive.controls.neokii.cruise_state_manager import CruiseStateManager
from selfdrive.controls.neokii.navi_controller import SpeedLimiter

hyundai_checksum = crcmod.mkCrcFun(0x11D, initCrc=0xFD, rev=False, xorOut=0xdf)

def create_mdps12(packer, frame, mdps12):
  values = mdps12
  values["CF_Mdps_ToiActive"] = 0
  values["CF_Mdps_ToiUnavail"] = 1
  values["CF_Mdps_MsgCount2"] = frame % 0x100
  values["CF_Mdps_Chksum2"] = 0

  dat = packer.make_can_msg("MDPS12", 2, values)[2]
  checksum = sum(dat) % 256
  values["CF_Mdps_Chksum2"] = checksum

  return packer.make_can_msg("MDPS12", 2, values)

def create_acc_commands(packer, enabled, accel, upper_jerk, idx, lead_visible,
                        set_speed, stopping, long_override, CS, stock_cam):
  commands = []

  cruise_enabled = enabled and CS.out.cruiseState.enabled

  values = CS.scc11
  values["MainMode_ACC"] = CS.out.cruiseState.available
  values["TauGapSet"] = CS.out.cruiseState.gapAdjust
  values["VSetDis"] = set_speed if cruise_enabled else 0
  values["AliveCounterACC"] = idx % 0x10
  values["ObjValid"] = 1 # close lead makes controls tighter

  if CruiseStateManager.instance().cruise_state_control:
    values["DriverAlertDisplay"] = 0

  #values["ACC_ObjStatus"] = 1,  # close lead makes controls tighter
  #values["ACC_ObjLatPos"] = 0,
  #values["ACC_ObjRelSpd"] = 10,
  #values["ACC_ObjDist"] = 50,  # close lead makes controls tighter

  if not stock_cam:
    active_cam = SpeedLimiter.instance().get_cam_active()
    values["Navi_SCC_Camera_Act"] = 2 if active_cam else 0
    values["Navi_SCC_Camera_Status"] = 2 if active_cam else 0

  commands.append(packer.make_can_msg("SCC11", 0, values))

  values = CS.scc12
  values["ACCMode"] = 2 if cruise_enabled and long_override else 1 if cruise_enabled else 0
  values["StopReq"] = 1 if cruise_enabled and stopping else 0
  values["aReqRaw"] = accel
  values["aReqValue"] = accel
  values["CR_VSM_Alive"] = idx % 0xF
  values["CR_VSM_ChkSum"] = 0
  scc12_dat = packer.make_can_msg("SCC12", 0, values)[2]
  values["CR_VSM_ChkSum"] = 0x10 - sum(sum(divmod(i, 16)) for i in scc12_dat) % 0x10

  commands.append(packer.make_can_msg("SCC12", 0, values))

  if CS.scc14 is not None:
    obj_gap = 2 if lead_visible else 0

    # TODO
    #lead = self.scc_smoother.get_lead(controls.sm)
    #if lead is not None:
    #  d = lead.dRel
    #  obj_gap = 1 if d < 25 else 2 if d < 40 else 3 if d < 60 else 4 if d < 80 else 5

    values = CS.scc14

    if enabled:
      values["ACCMode"] = 2 if CS.out.gasPressed and (accel > -0.2) else 1
      values["ObjGap"] = obj_gap
      if stopping:
        values["JerkUpperLimit"] = 0.5
        values["JerkLowerLimit"] = 10.
        values["ComfortBandUpper"] = 0.
        values["ComfortBandLower"] = 0.
        if CS.out.vEgo > 0.27:
          values["ComfortBandUpper"] = 2.
          values["ComfortBandLower"] = 0.
      else:
        values["JerkUpperLimit"] = 50.
        values["JerkLowerLimit"] = 50.
        values["ComfortBandUpper"] = 50.
        values["ComfortBandLower"] = 50.

    commands.append(packer.make_can_msg("SCC14", 0, values))

  return commands

def create_acc_opt(packer, CS):
  return packer.make_can_msg("SCC13", 0, CS.scc13)

