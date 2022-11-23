from selfdrive.car import STD_CARGO_KG
from selfdrive.car.hyundai.values_community import CAR
from common.conversions import Conversions as CV

def get_params(candidate, ret):
  tire_stiffness_factor = 0.8
  ret.steerRatio = 16.

  if candidate in CAR.ELANTRA_GT_I30:
    ret.mass = 1275. + STD_CARGO_KG
    ret.wheelbase = 2.7
    ret.steerRatio = 16.
    tire_stiffness_factor = 0.7
    ret.centerToFront = ret.wheelbase * 0.4
  elif candidate in [CAR.GRANDEUR_IG, CAR.GRANDEUR_IG_HEV]:
    ret.mass = 1570. + STD_CARGO_KG
    ret.wheelbase = 2.845
    ret.steerRatio = 16.
    tire_stiffness_factor = 0.8
    ret.centerToFront = ret.wheelbase * 0.385
  elif candidate in [CAR.GRANDEUR_IG_FL, CAR.GRANDEUR_IG_FL_HEV]:
    ret.mass = 1600. + STD_CARGO_KG
    ret.wheelbase = 2.885
    ret.steerRatio = 17.
    tire_stiffness_factor = 0.8
    ret.centerToFront = ret.wheelbase * 0.385
  elif candidate == CAR.GENESIS_EQ900:
    ret.mass = 2200
    ret.wheelbase = 3.15
    ret.steerRatio = 16.0
    ret.steerActuatorDelay = 0.075
  elif candidate == CAR.GENESIS_EQ900_L:
    ret.mass = 2290
    ret.wheelbase = 3.45
  elif candidate == CAR.GENESIS_G90_2019:
    ret.mass = 2150
    ret.wheelbase = 3.16
  elif candidate == CAR.MOHAVE:
    ret.mass = 2285. + STD_CARGO_KG
    ret.wheelbase = 2.895
  elif candidate in [CAR.K5, CAR.K5_HEV]:
    ret.mass = 3558. * CV.LB_TO_KG
    ret.wheelbase = 2.80
    ret.steerRatio = 15.5
    tire_stiffness_factor = 0.7
  elif candidate == CAR.K5_HEV_2022:
    ret.mass = 1515. + STD_CARGO_KG
    ret.wheelbase = 2.85
    ret.steerRatio = 15.5
    tire_stiffness_factor = 0.7
  elif candidate in [CAR.K7, CAR.K7_HEV]:
    ret.mass = 1850. + STD_CARGO_KG
    ret.wheelbase = 2.855
    ret.steerRatio = 15.5
    tire_stiffness_factor = 0.7
  elif candidate == CAR.K9:
    ret.mass = 2075. + STD_CARGO_KG
    ret.wheelbase = 3.15
    ret.steerRatio = 14.5
    tire_stiffness_factor = 0.8

  return tire_stiffness_factor
