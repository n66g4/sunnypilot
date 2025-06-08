#!/usr/bin/env python3
import bisect
import math
import os
from enum import IntEnum
from collections.abc import Callable

from cereal import log, car
import cereal.messaging as messaging
from openpilot.common.conversions import Conversions as CV
from openpilot.common.git import get_short_branch
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.locationd.calibrationd import MIN_SPEED_FILTER

AlertSize = log.ControlsState.AlertSize
AlertStatus = log.ControlsState.AlertStatus
VisualAlert = car.CarControl.HUDControl.VisualAlert
AudibleAlert = car.CarControl.HUDControl.AudibleAlert
EventName = car.CarEvent.EventName


# Alert priorities
class Priority(IntEnum):
  LOWEST = 0
  LOWER = 1
  LOW = 2
  MID = 3
  HIGH = 4
  HIGHEST = 5


# Event types
class ET:
  ENABLE = 'enable'
  PRE_ENABLE = 'preEnable'
  OVERRIDE_LATERAL = 'overrideLateral'
  OVERRIDE_LONGITUDINAL = 'overrideLongitudinal'
  NO_ENTRY = 'noEntry'
  WARNING = 'warning'
  USER_DISABLE = 'userDisable'
  SOFT_DISABLE = 'softDisable'
  IMMEDIATE_DISABLE = 'immediateDisable'
  PERMANENT = 'permanent'


# get event name from enum
EVENT_NAME = {v: k for k, v in EventName.schema.enumerants.items()}


class Events:
  def __init__(self):
    self.events: list[int] = []
    self.static_events: list[int] = []
    self.event_counters = dict.fromkeys(EVENTS.keys(), 0)

  @property
  def names(self) -> list[int]:
    return self.events

  def __len__(self) -> int:
    return len(self.events)

  def add(self, event_name: int, static: bool=False) -> None:
    if static:
      bisect.insort(self.static_events, event_name)
    bisect.insort(self.events, event_name)

  def clear(self) -> None:
    self.event_counters = {k: (v + 1 if k in self.events else 0) for k, v in self.event_counters.items()}
    self.events = self.static_events.copy()

  def contains(self, event_type: str) -> bool:
    return any(event_type in EVENTS.get(e, {}) for e in self.events)

  def create_alerts(self, event_types: list[str], callback_args=None):
    if callback_args is None:
      callback_args = []

    ret = []
    for e in self.events:
      types = EVENTS[e].keys()
      for et in event_types:
        if et in types:
          alert = EVENTS[e][et]
          if not isinstance(alert, Alert):
            alert = alert(*callback_args)

          if DT_CTRL * (self.event_counters[e] + 1) >= alert.creation_delay:
            alert.alert_type = f"{EVENT_NAME[e]}/{et}"
            alert.event_type = et
            ret.append(alert)
    return ret

  def add_from_msg(self, events):
    for e in events:
      bisect.insort(self.events, e.name.raw)

  def to_msg(self):
    ret = []
    for event_name in self.events:
      event = car.CarEvent.new_message()
      event.name = event_name
      for event_type in EVENTS.get(event_name, {}):
        setattr(event, event_type, True)
      ret.append(event)
    return ret


class Alert:
  def __init__(self,
               alert_text_1: str,
               alert_text_2: str,
               alert_status: log.ControlsState.AlertStatus,
               alert_size: log.ControlsState.AlertSize,
               priority: Priority,
               visual_alert: car.CarControl.HUDControl.VisualAlert,
               audible_alert: car.CarControl.HUDControl.AudibleAlert,
               duration: float,
               alert_rate: float = 0.,
               creation_delay: float = 0.):

    self.alert_text_1 = alert_text_1
    self.alert_text_2 = alert_text_2
    self.alert_status = alert_status
    self.alert_size = alert_size
    self.priority = priority
    self.visual_alert = visual_alert
    self.audible_alert = audible_alert

    self.duration = int(duration / DT_CTRL)

    self.alert_rate = alert_rate
    self.creation_delay = creation_delay

    self.alert_type = ""
    self.event_type: str | None = None

  def __str__(self) -> str:
    return f"{self.alert_text_1}/{self.alert_text_2} {self.priority} {self.visual_alert} {self.audible_alert}"

  def __gt__(self, alert2) -> bool:
    if not isinstance(alert2, Alert):
      return False
    return self.priority > alert2.priority


class NoEntryAlert(Alert):
  def __init__(self, alert_text_2: str,
               alert_text_1: str = "openpilot 不可用",
               visual_alert: car.CarControl.HUDControl.VisualAlert=VisualAlert.none):
    super().__init__(alert_text_1, alert_text_2, AlertStatus.normal,
                     AlertSize.mid, Priority.LOW, visual_alert,
                     AudibleAlert.refuse, 3.)


class SoftDisableAlert(Alert):
  def __init__(self, alert_text_2: str):
    super().__init__("立即接管控制", alert_text_2,
                     AlertStatus.userPrompt, AlertSize.full,
                     Priority.MID, VisualAlert.steerRequired,
                     AudibleAlert.warningSoft, 2.),


# less harsh version of SoftDisable, where the condition is user-triggered
class UserSoftDisableAlert(SoftDisableAlert):
  def __init__(self, alert_text_2: str):
    super().__init__(alert_text_2),
    self.alert_text_1 = "openpilot 将退出"


class ImmediateDisableAlert(Alert):
  def __init__(self, alert_text_2: str):
    super().__init__("立即接管控制", alert_text_2,
                     AlertStatus.critical, AlertSize.full,
                     Priority.HIGHEST, VisualAlert.steerRequired,
                     AudibleAlert.warningImmediate, 4.),


class EngagementAlert(Alert):
  def __init__(self, audible_alert: car.CarControl.HUDControl.AudibleAlert):
    super().__init__("", "",
                     AlertStatus.normal, AlertSize.none,
                     Priority.MID, VisualAlert.none,
                     audible_alert, .2),


class NormalPermanentAlert(Alert):
  def __init__(self, alert_text_1: str, alert_text_2: str = "", duration: float = 0.2, priority: Priority = Priority.LOWER, creation_delay: float = 0.):
    super().__init__(alert_text_1, alert_text_2,
                     AlertStatus.normal, AlertSize.mid if len(alert_text_2) else AlertSize.small,
                     priority, VisualAlert.none, AudibleAlert.none, duration, creation_delay=creation_delay),


class StartupAlert(Alert):
  def __init__(self, alert_text_1: str, alert_text_2: str = "请始终保持双手在方向盘上，眼睛注视道路", alert_status=AlertStatus.normal):
    super().__init__(alert_text_1, alert_text_2,
                     alert_status, AlertSize.mid,
                     Priority.LOWER, VisualAlert.none, AudibleAlert.none, 5.),


# ********** helper functions **********
def get_display_speed(speed_ms: float, metric: bool) -> str:
  speed = int(round(speed_ms * (CV.MS_TO_KPH if metric else CV.MS_TO_MPH)))
  unit = 'km/h' if metric else 'mph'
  return f"{speed} {unit}"


# ********** alert callback functions **********

AlertCallbackType = Callable[[car.CarParams, car.CarState, messaging.SubMaster, bool, int], Alert]


def soft_disable_alert(alert_text_2: str) -> AlertCallbackType:
  def func(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
    if soft_disable_time < int(0.5 / DT_CTRL):
      return ImmediateDisableAlert(alert_text_2)
    return SoftDisableAlert(alert_text_2)
  return func

def user_soft_disable_alert(alert_text_2: str) -> AlertCallbackType:
  def func(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
    if soft_disable_time < int(0.5 / DT_CTRL):
      return ImmediateDisableAlert(alert_text_2)
    return UserSoftDisableAlert(alert_text_2)
  return func

def startup_master_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  branch = get_short_branch()  # Ensure get_short_branch is cached to avoid lags on startup
  if "REPLAY" in os.environ:
    branch = "replay"

  return StartupAlert("警告：请随时准备接管控制", branch, alert_status=AlertStatus.userPrompt)

def below_engage_speed_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  return NoEntryAlert(f"请将车速提升至 {get_display_speed(CP.minEnableSpeed, metric)} 以上以启用")


def below_steer_speed_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  return Alert(
    f"车速低于 {get_display_speed(CP.minSteerSpeed, metric)} 时无法转向",
    "",
    AlertStatus.userPrompt, AlertSize.small,
    Priority.LOW, VisualAlert.steerRequired, AudibleAlert.prompt, 0.4)


def calibration_incomplete_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  first_word = '重新校准' if sm['liveCalibration'].calStatus == log.LiveCalibrationData.Status.recalibrating else '校准'
  return Alert(
    f"{first_word}进度：{sm['liveCalibration'].calPerc:.0f}%",
    f"请将车速提升至 {get_display_speed(MIN_SPEED_FILTER, metric)} 以上",
    AlertStatus.normal, AlertSize.mid,
    Priority.LOWEST, VisualAlert.none, AudibleAlert.none, .2)


def torque_nn_load_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  model_name = CP.lateralTuning.torque.nnModelName
  if model_name in ("", "mock"):
    return Alert(
      "神经网络横向控制器未加载",
      '⚙️ -> "sunnypilot" 获取更多信息',
      AlertStatus.userPrompt, AlertSize.mid,
      Priority.LOW, VisualAlert.none, AudibleAlert.prompt, 6.0)
  else:
    fuzzy = CP.lateralTuning.torque.nnModelFuzzyMatch
    alert_text_2 = '⚙️ -> "sunnypilot" 获取更多信息 [匹配 = 模糊]' if fuzzy else ""
    alert_status = AlertStatus.userPrompt if fuzzy else AlertStatus.normal
    alert_size = AlertSize.mid if fuzzy else AlertSize.small
    audible_alert = AudibleAlert.prompt if fuzzy else AudibleAlert.none
    return Alert(
      "神经网络横向控制器已加载",
      alert_text_2,
      alert_status, alert_size,
      Priority.LOW, VisualAlert.none, audible_alert, 6.0)

# *** debug alerts ***

def out_of_space_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  full_perc = round(100. - sm['deviceState'].freeSpacePercent)
  return NormalPermanentAlert("存储空间不足", f"已使用 {full_perc}%")


def posenet_invalid_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  mdl = sm['modelV2'].velocity.x[0] if len(sm['modelV2'].velocity.x) else math.nan
  err = CS.vEgo - mdl
  msg = f"速度误差：{err:.1f} m/s"
  return NoEntryAlert(msg, alert_text_1="姿态网络速度无效")


def process_not_running_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  not_running = [p.name for p in sm['managerState'].processes if not p.running and p.shouldBeRunning]
  msg = ', '.join(not_running)
  return NoEntryAlert(msg, alert_text_1="进程未运行")


def comm_issue_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  bs = [s for s in sm.data.keys() if not sm.all_checks([s, ])]
  msg = ', '.join(bs[:4])  # can't fit too many on one line
  return NoEntryAlert(msg, alert_text_1="进程间通信问题")


def camera_malfunction_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  all_cams = ('roadCameraState', 'driverCameraState', 'wideRoadCameraState')
  bad_cams = [s.replace('State', '') for s in all_cams if s in sm.data.keys() and not sm.all_checks([s, ])]
  return NormalPermanentAlert("摄像头故障", ', '.join(bad_cams))


def calibration_invalid_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  rpy = sm['liveCalibration'].rpyCalib
  yaw = math.degrees(rpy[2] if len(rpy) == 3 else math.nan)
  pitch = math.degrees(rpy[1] if len(rpy) == 3 else math.nan)
  angles = f"请重新安装设备 (俯仰角：{pitch:.1f}°，偏航角：{yaw:.1f}°)"
  return NormalPermanentAlert("校准无效", angles)


def overheat_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  cpu = max(sm['deviceState'].cpuTempC, default=0.)
  gpu = max(sm['deviceState'].gpuTempC, default=0.)
  temp = max((cpu, gpu, sm['deviceState'].memoryTempC))
  return NormalPermanentAlert("系统过热", f"{temp:.0f} °C")


def low_memory_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  return NormalPermanentAlert("内存不足", f"已使用 {sm['deviceState'].memoryUsagePercent}%")


def high_cpu_usage_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  x = max(sm['deviceState'].cpuUsagePercent, default=0.)
  return NormalPermanentAlert("CPU 使用率过高", f"已使用 {x}%")


def modeld_lagging_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  return NormalPermanentAlert("驾驶模型延迟", f"已丢弃 {sm['modelV2'].frameDropPerc:.1f}% 的帧")


def wrong_car_mode_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  text = "请启用自适应巡航以启用"
  if CP.carName == "honda":
    text = "请启用主开关以启用"
  return NoEntryAlert(text)


def joystick_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  axes = sm['testJoystick'].axes
  gb, steer = list(axes)[:2] if len(axes) else (0., 0.)
  vals = f"油门：{round(gb * 100.)}%，转向：{round(steer * 100.)}%"
  return NormalPermanentAlert("摇杆模式", vals)

def speed_limit_adjust_alert(CP: car.CarParams, CS: car.CarState, sm: messaging.SubMaster, metric: bool, soft_disable_time: int) -> Alert:
  speedLimit = sm['longitudinalPlanSP'].speedLimit
  speed = round(speedLimit * (CV.MS_TO_KPH if metric else CV.MS_TO_MPH))
  message = f'正在调整至限速 {speed} {"公里/小时" if metric else "英里/小时"}'
  return Alert(
    message,
    "",
    AlertStatus.normal, AlertSize.small,
    Priority.LOW, VisualAlert.none, AudibleAlert.none, 4.)



EVENTS: dict[int, dict[str, Alert | AlertCallbackType]] = {
  # ********** events with no alerts **********

  EventName.stockFcw: {},
  EventName.actuatorsApiUnavailable: {},

  # ********** events only containing alerts displayed in all states **********

  EventName.joystickDebug: {
    ET.WARNING: joystick_alert,
    ET.PERMANENT: NormalPermanentAlert("摇杆模式"),
  },

  EventName.controlsInitializing: {
    ET.NO_ENTRY: NoEntryAlert("系统正在初始化"),
  },

  EventName.startup: {
    ET.PERMANENT: StartupAlert("请随时准备接管控制")
  },

  EventName.startupMaster: {
    ET.PERMANENT: startup_master_alert,
  },

  # Car is recognized, but marked as dashcam only
  EventName.startupNoControl: {
    ET.PERMANENT: StartupAlert("行车记录仪模式"),
    ET.NO_ENTRY: NoEntryAlert("行车记录仪模式"),
  },

  # Car is not recognized
  EventName.startupNoCar: {
    ET.PERMANENT: StartupAlert("不支持的车辆，行车记录仪模式"),
  },

  EventName.startupNoFw: {
    ET.PERMANENT: StartupAlert("车辆未识别",
                               "请检查 comma power 连接",
                               alert_status=AlertStatus.userPrompt),
  },

  EventName.dashcamMode: {
    ET.PERMANENT: NormalPermanentAlert("行车记录仪模式",
                                       priority=Priority.LOWEST),
  },

  EventName.invalidLkasSetting: {
    ET.PERMANENT: NormalPermanentAlert("原厂 LKAS 已开启",
                                       "请关闭原厂 LKAS 以启用"),
  },

  EventName.cruiseMismatch: {
    #ET.PERMANENT: ImmediateDisableAlert("openpilot 无法取消巡航"),
  },

  # openpilot doesn't recognize the car. This switches openpilot into a
  # read-only mode. This can be solved by adding your fingerprint.
  # See https://github.com/commaai/openpilot/wiki/Fingerprinting for more information
  EventName.carUnrecognized: {
    ET.PERMANENT: NormalPermanentAlert("行车记录仪模式",
                                       '⚙️ -> "车辆" 选择您的车辆',
                                       priority=Priority.LOWEST),
  },

  EventName.stockAeb: {
    ET.PERMANENT: Alert(
      "刹车！",
      "原厂 AEB：存在碰撞风险",
      AlertStatus.critical, AlertSize.full,
      Priority.HIGHEST, VisualAlert.fcw, AudibleAlert.none, 2.),
    ET.NO_ENTRY: NoEntryAlert("原厂 AEB：存在碰撞风险"),
  },

  EventName.fcw: {
    ET.PERMANENT: Alert(
      "刹车！",
      "存在碰撞风险",
      AlertStatus.critical, AlertSize.full,
      Priority.HIGHEST, VisualAlert.fcw, AudibleAlert.warningSoft, 2.),
  },

  EventName.ldw: {
    ET.PERMANENT: Alert(
      "车道偏离检测",
      "",
      AlertStatus.userPrompt, AlertSize.small,
      Priority.LOW, VisualAlert.ldw, AudibleAlert.prompt, 3.),
  },

  # ********** events only containing alerts that display while engaged **********

  EventName.steerTempUnavailableSilent: {
    ET.WARNING: Alert(
      "转向暂时不可用",
      "",
      AlertStatus.userPrompt, AlertSize.small,
      Priority.LOW, VisualAlert.steerRequired, AudibleAlert.prompt, 1.8),
  },

  EventName.preDriverDistracted: {
    ET.PERMANENT: Alert(
      "请注意前方",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, .1),
  },

  EventName.promptDriverDistracted: {
    ET.PERMANENT: Alert(
      "请注意前方",
      "驾驶员分心",
      AlertStatus.userPrompt, AlertSize.mid,
      Priority.MID, VisualAlert.steerRequired, AudibleAlert.promptDistracted, .1),
  },

  EventName.driverDistracted: {
    ET.PERMANENT: Alert(
      "立即接管控制",
      "驾驶员分心",
      AlertStatus.critical, AlertSize.full,
      Priority.HIGH, VisualAlert.steerRequired, AudibleAlert.warningImmediate, .1),
  },

  EventName.preDriverUnresponsive: {
    ET.PERMANENT: Alert(
      "请触摸方向盘：未检测到人脸",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOW, VisualAlert.steerRequired, AudibleAlert.none, .1, alert_rate=0.75),
  },

  EventName.promptDriverUnresponsive: {
    ET.PERMANENT: Alert(
      "请触摸方向盘",
      "驾驶员无响应",
      AlertStatus.userPrompt, AlertSize.mid,
      Priority.MID, VisualAlert.steerRequired, AudibleAlert.promptDistracted, .1),
  },

  EventName.driverUnresponsive: {
    ET.PERMANENT: Alert(
      "立即接管控制",
      "驾驶员无响应",
      AlertStatus.critical, AlertSize.full,
      Priority.HIGH, VisualAlert.steerRequired, AudibleAlert.warningImmediate, .1),
  },

  EventName.preKeepHandsOnWheel: {
    ET.WARNING: Alert(
      "未检测到双手在方向盘上",
      "",
      AlertStatus.userPrompt, AlertSize.small,
      Priority.MID, VisualAlert.steerRequired, AudibleAlert.none, .1, alert_rate=0.75),
  },

  EventName.promptKeepHandsOnWheel: {
    ET.WARNING: Alert(
      "双手离开方向盘",
      "请将双手放在方向盘上",
      AlertStatus.critical, AlertSize.mid,
      Priority.MID, VisualAlert.steerRequired, AudibleAlert.promptDistracted, .1),
  },

  EventName.keepHandsOnWheel: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("驾驶员持续双手离开方向盘"),
  },

  EventName.manualRestart: {
    ET.WARNING: Alert(
      "接管控制",
      "请手动恢复驾驶",
      AlertStatus.userPrompt, AlertSize.mid,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, .2),
  },

  EventName.resumeRequired: {
    ET.WARNING: Alert(
      "请按恢复按钮退出停车状态",
      "",
      AlertStatus.userPrompt, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, .2),
  },

  EventName.belowSteerSpeed: {
    ET.WARNING: below_steer_speed_alert,
  },

  EventName.preLaneChangeLeft: {
    ET.WARNING: Alert(
      "向左转向开始变道（确保安全）",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, .1, alert_rate=0.75),
  },

  EventName.preLaneChangeRight: {
    ET.WARNING: Alert(
      "向右转向开始变道（确保安全）",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, .1, alert_rate=0.75),
  },

  EventName.laneChangeBlocked: {
    ET.WARNING: Alert(
      "盲区检测到车辆",
      "",
      AlertStatus.userPrompt, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.prompt, .1),
  },

  EventName.laneChangeRoadEdge: {
    ET.WARNING: Alert(
      "无法变道：道路边缘",
      "",
      AlertStatus.userPrompt, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.prompt, .1),
  },

  EventName.laneChange: {
    ET.WARNING: Alert(
      "正在变道",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, .1),
  },

  EventName.manualSteeringRequired: {
    ET.WARNING: Alert(
      "自动车道居中已关闭",
      "需要手动转向",
      AlertStatus.normal, AlertSize.mid,
      Priority.LOW, VisualAlert.none, AudibleAlert.disengage, 1.),
  },

  EventName.manualLongitudinalRequired: {
    ET.WARNING: Alert(
      "智能/自适应巡航控制已关闭",
      "需要手动控制油门/刹车",
      AlertStatus.normal, AlertSize.mid,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, 1.),
  },

  EventName.cruiseEngageBlocked: {
    ET.WARNING: Alert(
      "openpilot 不可用",
      "巡航启用时踩下踏板",
      AlertStatus.normal, AlertSize.mid,
      Priority.LOW, VisualAlert.brakePressed, AudibleAlert.refuse, 3.),
  },

  EventName.steerSaturated: {
    ET.WARNING: Alert(
      "接管控制",
      "转向超出限制",
      AlertStatus.userPrompt, AlertSize.mid,
      Priority.LOW, VisualAlert.steerRequired, AudibleAlert.promptRepeat, 2.),
  },

  # Thrown when the fan is driven at >50% but is not rotating
  EventName.fanMalfunction: {
    ET.PERMANENT: NormalPermanentAlert("风扇故障", "可能的硬件问题"),
  },

  # Camera is not outputting frames
  EventName.cameraMalfunction: {
    ET.PERMANENT: camera_malfunction_alert,
    ET.SOFT_DISABLE: soft_disable_alert("摄像头故障"),
    ET.NO_ENTRY: NoEntryAlert("摄像头故障：请重启设备"),
  },
  # Camera framerate too low
  EventName.cameraFrameRate: {
    ET.PERMANENT: NormalPermanentAlert("摄像头帧率过低", "请重启设备"),
    ET.SOFT_DISABLE: soft_disable_alert("摄像头帧率过低"),
    ET.NO_ENTRY: NoEntryAlert("摄像头帧率过低：请重启设备"),
  },

  # Unused

  EventName.locationdTemporaryError: {
    ET.NO_ENTRY: NoEntryAlert("定位服务临时错误"),
    ET.SOFT_DISABLE: soft_disable_alert("定位服务临时错误"),
  },

  EventName.locationdPermanentError: {
    ET.NO_ENTRY: NoEntryAlert("定位服务永久错误"),
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("定位服务永久错误"),
    ET.PERMANENT: NormalPermanentAlert("定位服务永久错误"),
  },

  # openpilot tries to learn certain parameters about your car by observing
  # how the car behaves to steering inputs from both human and openpilot driving.
  # This includes:
  # - steer ratio: gear ratio of the steering rack. Steering angle divided by tire angle
  # - tire stiffness: how much grip your tires have
  # - angle offset: most steering angle sensors are offset and measure a non zero angle when driving straight
  # This alert is thrown when any of these values exceed a sanity check. This can be caused by
  # bad alignment or bad sensor data. If this happens consistently consider creating an issue on GitHub
  EventName.paramsdTemporaryError: {
    ET.NO_ENTRY: NoEntryAlert("参数服务临时错误"),
    ET.SOFT_DISABLE: soft_disable_alert("参数服务临时错误"),
  },

  EventName.paramsdPermanentError: {
    ET.NO_ENTRY: NoEntryAlert("参数服务永久错误"),
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("参数服务永久错误"),
    ET.PERMANENT: NormalPermanentAlert("参数服务永久错误"),
  },

  EventName.speedLimitActive: {
    ET.WARNING: Alert(
      "设定速度已调整为限速值",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, 3.),
  },

  EventName.speedLimitValueChange: {
    ET.WARNING: speed_limit_adjust_alert,
  },

  EventName.e2eLongStart: {
    ET.PERMANENT: Alert(
      "",
      "",
      AlertStatus.normal, AlertSize.none,
      Priority.MID, VisualAlert.none, AudibleAlert.promptStarting, 1.5),
  },

  EventName.speedLimitPreActive: {
    ET.WARNING: Alert(
      "",
      "",
      AlertStatus.normal, AlertSize.none,
      Priority.MID, VisualAlert.none, AudibleAlert.promptSingleLow, .45),
  },

  EventName.speedLimitConfirmed: {
    ET.WARNING: Alert(
      "",
      "",
      AlertStatus.normal, AlertSize.none,
      Priority.MID, VisualAlert.none, AudibleAlert.promptSingleHigh, .45),
  },

  # ********** events that affect controls state transitions **********

  EventName.pcmEnable: {
    ET.ENABLE: EngagementAlert(AudibleAlert.engage),
  },

  EventName.buttonEnable: {
    ET.ENABLE: EngagementAlert(AudibleAlert.engage),
  },

  EventName.silentButtonEnable: {
    ET.ENABLE: Alert(
      "",
      "",
      AlertStatus.normal, AlertSize.none,
      Priority.MID, VisualAlert.none, AudibleAlert.none, .2, 0., 0.),
  },

  EventName.pcmDisable: {
    ET.USER_DISABLE: EngagementAlert(AudibleAlert.disengage),
  },

  EventName.buttonCancel: {
    ET.USER_DISABLE: EngagementAlert(AudibleAlert.disengage),
    ET.NO_ENTRY: NoEntryAlert("已按下取消键"),
  },

  EventName.brakeHold: {
    ET.USER_DISABLE: EngagementAlert(AudibleAlert.disengage),
    ET.NO_ENTRY: NoEntryAlert("刹车保持已激活"),
  },

  EventName.silentBrakeHold: {
    ET.USER_DISABLE: Alert(
      "",
      "",
      AlertStatus.normal, AlertSize.none,
      Priority.MID, VisualAlert.none, AudibleAlert.none, .2, 0., 0.),
    ET.NO_ENTRY: NoEntryAlert("刹车保持已激活"),
  },

  EventName.spAutoBrakeHold: {
    ET.PERMANENT: Alert(
      "sunnypilot 刹车保持已激活",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOWEST, VisualAlert.none, AudibleAlert.prompt, 0.),
  },

  EventName.parkBrake: {
    ET.USER_DISABLE: EngagementAlert(AudibleAlert.disengage),
    ET.NO_ENTRY: NoEntryAlert("已启用驻车制动"),
  },

  EventName.pedalPressed: {
    ET.USER_DISABLE: EngagementAlert(AudibleAlert.disengage),
    ET.NO_ENTRY: NoEntryAlert("已踩下踏板",
                              visual_alert=VisualAlert.brakePressed),
  },

  EventName.silentPedalPressed: {
    ET.USER_DISABLE: Alert(
      "",
      "",
      AlertStatus.normal, AlertSize.none,
      Priority.MID, VisualAlert.none, AudibleAlert.none, .2),
    ET.NO_ENTRY: NoEntryAlert("尝试时踩下踏板",
                              visual_alert=VisualAlert.brakePressed),
  },

  EventName.preEnableStandstill: {
    ET.PRE_ENABLE: Alert(
      "松开刹车以启用",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOWEST, VisualAlert.none, AudibleAlert.none, .1, creation_delay=1.),
  },

  EventName.gasPressedOverride: {
    ET.OVERRIDE_LONGITUDINAL: Alert(
      "",
      "",
      AlertStatus.normal, AlertSize.none,
      Priority.LOWEST, VisualAlert.none, AudibleAlert.none, .1),
  },

  EventName.steerOverride: {
    ET.OVERRIDE_LATERAL: Alert(
      "",
      "",
      AlertStatus.normal, AlertSize.none,
      Priority.LOWEST, VisualAlert.none, AudibleAlert.none, .1),
  },

  EventName.wrongCarMode: {
    ET.USER_DISABLE: EngagementAlert(AudibleAlert.disengage),
    ET.NO_ENTRY: wrong_car_mode_alert,
  },

  EventName.resumeBlocked: {
    ET.NO_ENTRY: NoEntryAlert("请按 Set 键以启用"),
  },

  EventName.wrongCruiseMode: {
    ET.USER_DISABLE: EngagementAlert(AudibleAlert.disengage),
    ET.NO_ENTRY: NoEntryAlert("自适应巡航已禁用"),
  },

  EventName.steerTempUnavailable: {
    ET.SOFT_DISABLE: soft_disable_alert("转向暂时不可用"),
    ET.NO_ENTRY: NoEntryAlert("转向暂时不可用"),
  },

  EventName.steerTimeLimit: {
    ET.SOFT_DISABLE: soft_disable_alert("车辆转向时间限制"),
    ET.NO_ENTRY: NoEntryAlert("车辆转向时间限制"),
  },

  EventName.outOfSpace: {
    ET.PERMANENT: out_of_space_alert,
    ET.NO_ENTRY: NoEntryAlert("存储空间不足"),
  },

  EventName.belowEngageSpeed: {
    ET.NO_ENTRY: below_engage_speed_alert,
  },

  EventName.sensorDataInvalid: {
    ET.PERMANENT: Alert(
      "传感器数据无效",
      "可能的硬件问题",
      AlertStatus.normal, AlertSize.mid,
      Priority.LOWER, VisualAlert.none, AudibleAlert.none, .2, creation_delay=1.),
    ET.NO_ENTRY: NoEntryAlert("传感器数据无效"),
    ET.SOFT_DISABLE: soft_disable_alert("传感器数据无效"),
  },

  EventName.noGps: {
    ET.PERMANENT: Alert(
      "GPS 信号弱",
      "请确保设备无遮挡，能看到天空",
      AlertStatus.normal, AlertSize.mid,
      Priority.LOWER, VisualAlert.none, AudibleAlert.none, .2, creation_delay=600.)
  },

  EventName.soundsUnavailable: {
    ET.PERMANENT: NormalPermanentAlert("未检测到扬声器", "请重启设备"),
    ET.NO_ENTRY: NoEntryAlert("未检测到扬声器"),
  },

  EventName.tooDistracted: {
    ET.NO_ENTRY: NoEntryAlert("分心等级过高"),
  },

  EventName.overheat: {
    ET.PERMANENT: overheat_alert,
    ET.SOFT_DISABLE: soft_disable_alert("系统过热"),
    ET.NO_ENTRY: NoEntryAlert("系统过热"),
  },

  EventName.wrongGear: {
    ET.SOFT_DISABLE: user_soft_disable_alert("挡位非 D 档"),
    ET.NO_ENTRY: NoEntryAlert("挡位非 D 档"),
  },

  EventName.silentWrongGear: {
    ET.SOFT_DISABLE: Alert(
      "挡位非 D 档",
      "openpilot 不可用",
      AlertStatus.normal, AlertSize.mid,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, 0., 2., 3.),
    ET.NO_ENTRY: Alert(
      "挡位非 D 档",
      "openpilot 不可用",
      AlertStatus.normal, AlertSize.mid,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, 0., 2., 3.),
  },

  # This alert is thrown when the calibration angles are outside of the acceptable range.
  # For example if the device is pointed too much to the left or the right.
  # Usually this can only be solved by removing the mount from the windshield completely,
  # and attaching while making sure the device is pointed straight forward and is level.
  # See https://comma.ai/setup for more information
  EventName.calibrationInvalid: {
    ET.PERMANENT: calibration_invalid_alert,
    ET.SOFT_DISABLE: soft_disable_alert("校准无效：请重新安装设备并重新校准"),
    ET.NO_ENTRY: NoEntryAlert("校准无效：请重新安装设备并重新校准"),
  },

  EventName.calibrationIncomplete: {
    ET.PERMANENT: calibration_incomplete_alert,
    ET.SOFT_DISABLE: soft_disable_alert("校准未完成"),
    ET.NO_ENTRY: NoEntryAlert("校准进行中"),
  },

  EventName.calibrationRecalibrating: {
    ET.PERMANENT: calibration_incomplete_alert,
    ET.SOFT_DISABLE: soft_disable_alert("检测到设备重新安装：正在重新校准"),
    ET.NO_ENTRY: NoEntryAlert("检测到重新安装：正在重新校准"),
  },

  EventName.doorOpen: {
    ET.SOFT_DISABLE: user_soft_disable_alert("车门未关闭"),
    ET.NO_ENTRY: NoEntryAlert("车门未关闭"),
  },

  EventName.seatbeltNotLatched: {
    ET.SOFT_DISABLE: user_soft_disable_alert("安全带未系好"),
    ET.NO_ENTRY: NoEntryAlert("安全带未系好"),
  },

  EventName.espDisabled: {
    ET.SOFT_DISABLE: soft_disable_alert("电子稳定控制已禁用"),
    ET.NO_ENTRY: NoEntryAlert("电子稳定控制已禁用"),
  },

  EventName.lowBattery: {
    ET.SOFT_DISABLE: soft_disable_alert("电池电量低"),
    ET.NO_ENTRY: NoEntryAlert("电池电量低"),
  },

  # Different openpilot services communicate between each other at a certain
  # interval. If communication does not follow the regular schedule this alert
  # is thrown. This can mean a service crashed, did not broadcast a message for
  # ten times the regular interval, or the average interval is more than 10% too high.
  EventName.commIssue: {
    ET.SOFT_DISABLE: soft_disable_alert("进程间通信问题"),
    ET.NO_ENTRY: comm_issue_alert,
  },
  EventName.commIssueAvgFreq: {
    ET.SOFT_DISABLE: soft_disable_alert("进程间通信率低"),
    ET.NO_ENTRY: NoEntryAlert("进程间通信率低"),
  },

  EventName.controlsdLagging: {
    ET.SOFT_DISABLE: soft_disable_alert("控制进程滞后"),
    ET.NO_ENTRY: NoEntryAlert("控制进程滞后：请重启设备"),
  },

  # Thrown when manager detects a service exited unexpectedly while driving
  EventName.processNotRunning: {
    ET.NO_ENTRY: process_not_running_alert,
    ET.SOFT_DISABLE: soft_disable_alert("进程未运行"),
  },

  EventName.radarFault: {
    ET.SOFT_DISABLE: soft_disable_alert("雷达错误：请重启车辆"),
    ET.NO_ENTRY: NoEntryAlert("雷达错误：请重启车辆"),
  },

  # Every frame from the camera should be processed by the model. If modeld
  # is not processing frames fast enough they have to be dropped. This alert is
  # thrown when over 20% of frames are dropped.
  EventName.modeldLagging: {
    ET.SOFT_DISABLE: soft_disable_alert("驾驶模型滞后"),
    ET.NO_ENTRY: NoEntryAlert("驾驶模型滞后"),
    ET.PERMANENT: modeld_lagging_alert,
  },

  # Besides predicting the path, lane lines and lead car data the model also
  # predicts the current velocity and rotation speed of the car. If the model is
  # very uncertain about the current velocity while the car is moving, this
  # usually means the model has trouble understanding the scene. This is used
  # as a heuristic to warn the driver.
  EventName.posenetInvalid: {
    ET.SOFT_DISABLE: soft_disable_alert("Posenet 速度无效"),
    ET.NO_ENTRY: posenet_invalid_alert,
  },

  # When the localizer detects an acceleration of more than 40 m/s^2 (~4G) we
  # alert the driver the device might have fallen from the windshield.
  EventName.deviceFalling: {
    ET.SOFT_DISABLE: soft_disable_alert("设备从支架上脱落"),
    ET.NO_ENTRY: NoEntryAlert("设备从支架上脱落"),
  },

  EventName.lowMemory: {
    ET.SOFT_DISABLE: soft_disable_alert("内存不足：请重启设备"),
    ET.PERMANENT: low_memory_alert,
    ET.NO_ENTRY: NoEntryAlert("内存不足：请重启设备"),
  },

  EventName.highCpuUsage: {
    #ET.SOFT_DISABLE: soft_disable_alert("系统故障：请重启设备"),
    #ET.PERMANENT: NormalPermanentAlert("系统故障", "请重启设备"),
    ET.NO_ENTRY: high_cpu_usage_alert,
  },

  EventName.accFaulted: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("巡航故障：请重启车辆"),
    ET.PERMANENT: NormalPermanentAlert("巡航故障：请重启车辆以启用"),
    ET.NO_ENTRY: NoEntryAlert("巡航故障：请重启车辆"),
  },

  EventName.espActive: {
    ET.SOFT_DISABLE: soft_disable_alert("电子稳定控制已激活"),
    ET.NO_ENTRY: NoEntryAlert("电子稳定控制已激活"),
  },

  EventName.controlsMismatch: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("控制不匹配"),
    ET.NO_ENTRY: NoEntryAlert("控制不匹配"),
  },

  EventName.controlsMismatchLong: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("控制不匹配\n纵向"),
    ET.NO_ENTRY: NoEntryAlert("控制不匹配\n纵向"),
  },

  EventName.roadCameraError: {
    ET.PERMANENT: NormalPermanentAlert("摄像头 CRC 错误 - 道路",
                                       duration=1.,
                                       creation_delay=30.),
  },

  EventName.wideRoadCameraError: {
    ET.PERMANENT: NormalPermanentAlert("摄像头 CRC 错误 - 道路鱼眼",
                                       duration=1.,
                                       creation_delay=30.),
  },

  EventName.driverCameraError: {
    ET.PERMANENT: NormalPermanentAlert("摄像头 CRC 错误 - 驾驶员",
                                       duration=1.,
                                       creation_delay=30.),
  },

  # Sometimes the USB stack on the device can get into a bad state
  # causing the connection to the panda to be lost
  EventName.usbError: {
    ET.SOFT_DISABLE: soft_disable_alert("USB 错误：请重启设备"),
    ET.PERMANENT: NormalPermanentAlert("USB 错误：请重启设备", ""),
    ET.NO_ENTRY: NoEntryAlert("USB 错误：请重启设备"),
  },

  # This alert can be thrown for the following reasons:
  # - No CAN data received at all
  # - CAN data is received, but some message are not received at the right frequency
  # If you're not writing a new car port, this is usually cause by faulty wiring
  EventName.canError: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("CAN 错误"),
    ET.PERMANENT: Alert(
      "CAN 错误：请检查连接",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, 1., creation_delay=1.),
    ET.NO_ENTRY: NoEntryAlert("CAN 错误：请检查连接"),
  },

  EventName.canBusMissing: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("CAN 总线断开"),
    ET.PERMANENT: Alert(
      "CAN 总线断开：可能是线缆故障",
      "",
      AlertStatus.normal, AlertSize.small,
      Priority.LOW, VisualAlert.none, AudibleAlert.none, 1., creation_delay=1.),
    ET.NO_ENTRY: NoEntryAlert("CAN 总线断开：请检查连接"),
  },

  EventName.steerUnavailable: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("LKAS 故障：请重启车辆"),
    ET.PERMANENT: NormalPermanentAlert("LKAS 故障：请重启车辆以启用"),
    ET.NO_ENTRY: NoEntryAlert("LKAS 故障：请重启车辆"),
  },

  EventName.reverseGear: {
    ET.PERMANENT: Alert(
      "倒车\n档位",
      "",
      AlertStatus.normal, AlertSize.full,
      Priority.LOWEST, VisualAlert.none, AudibleAlert.none, .2, creation_delay=0.5),
    ET.USER_DISABLE: ImmediateDisableAlert("倒车档位"),
    ET.NO_ENTRY: NoEntryAlert("倒车档位"),
  },

  EventName.spReverseGear: {
    ET.PERMANENT: Alert(
      "倒车\n档位",
      "",
      AlertStatus.normal, AlertSize.full,
      Priority.LOWEST, VisualAlert.none, AudibleAlert.none, .2, creation_delay=0.5),
    ET.NO_ENTRY: NoEntryAlert("倒车档位"),
  },

  # On cars that use stock ACC the car can decide to cancel ACC for various reasons.
  # When this happens we can no long control the car so the user needs to be warned immediately.
  EventName.cruiseDisabled: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("巡航已关闭"),
  },

  # For planning the trajectory Model Predictive Control (MPC) is used. This is
  # an optimization algorithm that is not guaranteed to find a feasible solution.
  # If no solution is found or the solution has a very high cost this alert is thrown.
  EventName.plannerErrorDEPRECATED: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("规划器解决方案错误"),
    ET.NO_ENTRY: NoEntryAlert("规划器解决方案错误"),
  },

  # When the relay in the harness box opens the CAN bus between the LKAS camera
  # and the rest of the car is separated. When messages from the LKAS camera
  # are received on the car side this usually means the relay hasn't opened correctly
  # and this alert is thrown.
  EventName.relayMalfunction: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("线束继电器故障"),
    ET.PERMANENT: NormalPermanentAlert("线束继电器故障", "请检查硬件"),
    ET.NO_ENTRY: NoEntryAlert("线束继电器故障"),
  },

  EventName.speedTooLow: {
    ET.IMMEDIATE_DISABLE: Alert(
      "sunnypilot 已取消",
      "速度过低",
      AlertStatus.normal, AlertSize.mid,
      Priority.HIGH, VisualAlert.none, AudibleAlert.disengage, 3.),
  },

  # When the car is driving faster than most cars in the training data, the model outputs can be unpredictable.
  EventName.speedTooHigh: {
    ET.WARNING: Alert(
      "速度过高",
      "模型在此速度下不可靠",
      AlertStatus.userPrompt, AlertSize.mid,
      Priority.HIGH, VisualAlert.steerRequired, AudibleAlert.promptRepeat, 4.),
    ET.NO_ENTRY: NoEntryAlert("请减速以启用"),
  },

  EventName.lowSpeedLockout: {
    ET.PERMANENT: NormalPermanentAlert("巡航故障：请重启车辆以启用"),
    ET.NO_ENTRY: NoEntryAlert("巡航故障：请重启车辆"),
  },

  EventName.lkasDisabled: {
    ET.PERMANENT: NormalPermanentAlert("LKAS 已禁用：请启用 LKAS 以启用"),
    ET.NO_ENTRY: NoEntryAlert("LKAS 已禁用"),
  },

  EventName.vehicleSensorsInvalid: {
    ET.IMMEDIATE_DISABLE: ImmediateDisableAlert("车辆传感器无效"),
    ET.PERMANENT: NormalPermanentAlert("车辆传感器正在校准", "请驾驶以校准"),
    ET.NO_ENTRY: NoEntryAlert("车辆传感器正在校准"),
  },

  EventName.torqueNNLoad: {
    ET.PERMANENT: torque_nn_load_alert,
  },

  EventName.hyundaiRadarTracksAvailable: {
    ET.PERMANENT: NormalPermanentAlert("雷达轨迹可用。请重启车辆以初始化")
  }
}


if __name__ == '__main__':
  # print all alerts by type and priority
  from cereal.services import SERVICE_LIST
  from collections import defaultdict

  event_names = {v: k for k, v in EventName.schema.enumerants.items()}
  alerts_by_type: dict[str, dict[Priority, list[str]]] = defaultdict(lambda: defaultdict(list))

  CP = car.CarParams.new_message()
  CS = car.CarState.new_message()
  sm = messaging.SubMaster(list(SERVICE_LIST.keys()))

  for i, alerts in EVENTS.items():
    for et, alert in alerts.items():
      if callable(alert):
        alert = alert(CP, CS, sm, False, 1)
      alerts_by_type[et][alert.priority].append(event_names[i])

  all_alerts: dict[str, list[tuple[Priority, list[str]]]] = {}
  for et, priority_alerts in alerts_by_type.items():
    all_alerts[et] = sorted(priority_alerts.items(), key=lambda x: x[0], reverse=True)

  for status, evs in sorted(all_alerts.items(), key=lambda x: x[0]):
    print(f"**** {status} ****")
    for p, alert_list in evs:
      print(f"  {repr(p)}:")
      print("   ", ', '.join(alert_list), "\n")
