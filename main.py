"""喝水小助手 MVP

定时提醒喝水，树苗成长 vs 枯萎做内生视觉反馈。
单文件实现，图形全部 QPainter 直接画，零外部素材。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, date, timedelta
from html import escape
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTime,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSystemTrayIcon,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


# PyInstaller 打包后 __file__ 指向临时解压目录，改用 sys.executable 的目录才能持久化数据
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
    # assets 在打包时会被 PyInstaller 复制到临时解压目录 (sys._MEIPASS)
    ASSETS_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "assets"
else:
    BASE_DIR = Path(__file__).parent
    ASSETS_DIR = BASE_DIR / "assets"

CONFIG_PATH = BASE_DIR / "config.json"
REMINDERS_PATH = BASE_DIR / "reminders.json"
DRINK_ENTRIES_PATH = BASE_DIR / "drink_entries.json"
HISTORY_PATH = BASE_DIR / "history.json"
START_BAT_PATH = BASE_DIR / "start.bat"
STARTUP_FOLDER = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
STARTUP_LINK_NAME = "喝水小助手.bat"
FOLLOW_UP_MIN = 5  # 推迟/不理后的再提醒延时（分钟）
RESPONSE_WINDOW_MIN = 5  # 提醒弹出后等响应的时长（分钟），超时视为不理
PROACTIVE_COOLDOWN_MIN = 20  # 主动记水冷却：距上次喝水记录不足这么久 → 拒记
IDLE_AWAY_MIN = 5  # 无键鼠输入超过这么久（分钟）视为人不在电脑前，挂起一切计时和惩罚
WAKE_CATCHUP_MIN = 30  # 离开超过这么久（分钟），回来时自动补弹一次提醒
SCHEMA_VERSION = 4
STAGE_COUNT = 9  # 0-8 共 9 档成长阶段（均匀 11.1% 一档）
SKIP_RESET_THRESHOLD = 3  # 连续跳过达到此值 → 视觉重置到种子（drunk_ml 保留）


def get_idle_seconds() -> float:
    """距最后一次键鼠输入的秒数（Windows GetLastInputInfo）。
    锁屏/熄屏/人离开都表现为无输入，统一按这个口径判定"人不在"。
    API 不可用（非 Windows / 调用失败）返回 0，等于永不判定离开。"""
    try:
        import ctypes

        class _LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = _LastInputInfo()
        lii.cbSize = ctypes.sizeof(_LastInputInfo)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        elapsed_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        # GetTickCount 32 位回绕（49.7 天）会出负数，按 0 处理
        return max(0.0, elapsed_ms / 1000.0)
    except Exception:
        return 0.0


def sync_startup_shortcut(enabled: bool) -> bool:
    """把启动脚本放到 Windows 启动文件夹或删除。返回是否操作成功。

    enabled=True：生成一个新的 .bat 到启动文件夹，内容里写死项目绝对路径
    enabled=False：从启动文件夹删除（如存在）

    注意：不能简单 copy start.bat 过去，因为 start.bat 用 %~dp0 引用自己所在
    目录。放到启动文件夹后 %~dp0 会解析成启动文件夹，找不到 main.py。
    所以这里另生成一份写死项目路径的 .bat。
    """
    dst = STARTUP_FOLDER / STARTUP_LINK_NAME
    try:
        if enabled:
            STARTUP_FOLDER.mkdir(parents=True, exist_ok=True)
            project_dir = Path(__file__).parent.resolve()
            # .bat 里的路径用双引号包起来，防中文/空格
            content = (
                "@echo off\r\n"
                f'cd /d "{project_dir}"\r\n'
                'start "" pythonw main.py\r\n'
            )
            # Windows .bat 用 GBK 系。项目路径无中文，用 gbk 兼容
            dst.write_text(content, encoding="gbk")
            return True
        else:
            if dst.exists():
                dst.unlink()
            return True
    except OSError:
        return False

# stage(0-8) × skip_count(0/1/2) → 资源图。
# s0 = 喝水后健康，s1 = 跳过 1 次微枯，s2 = 跳过 2 次严重枯萎
# stage9_s0 = 达成今日目标的果实图
STAGE_ASSETS = {
    stage: {skip: f"stage{stage}_s{skip}.jpg" for skip in range(3)}
    for stage in range(STAGE_COUNT)
}
FRUIT_ASSET = "stage9_s0.jpg"
CANVAS_BG_COLOR = "#faf1d6"  # 主窗口底色，跟画布左右裁切留白融合


# ----------------------------- 视觉主题 -----------------------------

# THEME：全项目色/字/间距/圆角唯一取值来源。QSS 由 f-string 拼接
THEME = {
    # 背景层
    "bg_canvas": "#faf1d6",
    "bg_surface": "#f6ead0",
    "bg_elevated": "#fffaea",
    # 文字层
    "text_primary": "#3d3220",
    "text_secondary": "#7a6a4a",
    "text_muted": "#a89877",
    # 边框
    "border_subtle": "#e8ddb5",
    "border_default": "#c9b98a",
    # 语义色
    "accent_leaf": "#7fa650",
    "accent_leaf_hi": "#95bb62",
    "accent_leaf_lo": "#6a8f42",
    "accent_bark": "#8b6a4a",
    "accent_bark_hi": "#a48260",
    "success_water": "#5b9bb5",
    "warning_dry": "#d4a05a",
    "danger_wilt": "#b0603a",
    # 动效
    "dur_fast": 120,
    "dur_normal": 220,
    "dur_slow": 380,
    # 交互开关
    "motion_enabled": True,
}

# 字体家族兜底链（LXGW WenKai 系统装了就用，否则降级到 Segoe UI/YaHei）
FONT_DISPLAY = '"LXGW WenKai", "Microsoft YaHei UI", "Segoe UI", sans-serif'
FONT_BODY = '"Microsoft YaHei UI", "Segoe UI", sans-serif'
FONT_MONO = '"JetBrains Mono", "Cascadia Mono", "Consolas", monospace'


def build_theme_qss() -> str:
    """集中样式表。所有 hex 从 THEME 取，禁止 QSS 里手写裸 hex。"""
    T = THEME
    return f"""
    QMainWindow {{
        background: {T["bg_canvas"]};
        color: {T["text_primary"]};
        font-family: {FONT_BODY};
    }}
    QWidget#central_widget {{
        background: {T["bg_canvas"]};
        color: {T["text_primary"]};
        font-family: {FONT_BODY};
    }}

    /* 顶部标题区 */
    QLabel#title_label {{
        font-family: {FONT_DISPLAY};
        font-size: 20px;
        font-weight: 700;
        color: {T["text_primary"]};
        background: transparent;
    }}
    QLabel#subtitle_label {{
        font-size: 13px;
        font-weight: 400;
        color: {T["text_secondary"]};
        background: transparent;
    }}
    QLabel#countdown_label {{
        font-size: 12px;
        font-weight: 500;
        color: {T["text_secondary"]};
        background: transparent;
    }}
    QLabel#reminder_hint_label {{
        font-family: {FONT_DISPLAY};
        font-size: 14px;
        font-weight: 400;
        color: {T["text_secondary"]};
        background: transparent;
    }}

    /* 行动区容器 */
    QWidget#action_zone {{
        background: {T["bg_surface"]};
        border: 1px solid {T["border_subtle"]};
        border-radius: 18px;
    }}

    /* 主按钮：accent_leaf 底 + 白字 */
    QPushButton#drink_primary_btn {{
        font-family: {FONT_DISPLAY};
        font-size: 15px;
        font-weight: 700;
        color: #ffffff;
        background: {T["accent_leaf"]};
        border: 1px solid {T["accent_leaf_lo"]};
        border-radius: 14px;
        padding: 5px 16px;
        min-height: 26px;
        outline: none;
    }}
    QPushButton#drink_primary_btn:hover {{
        background: {T["accent_leaf_hi"]};
        border: 1px solid {T["accent_leaf"]};
    }}
    QPushButton#drink_primary_btn:pressed {{
        background: {T["accent_leaf_lo"]};
        color: #f0f5df;
        border: 1px solid #547833;
    }}
    QPushButton#drink_primary_btn:disabled {{
        background: #d8d1b0;
        color: {T["text_muted"]};
        border: 1px solid {T["border_default"]};
    }}
    QPushButton#drink_primary_btn:focus {{
        border: 2px solid {T["accent_leaf"]};
    }}

    /* 次按钮：奶白底 + 暖棕描边字 */
    QPushButton#drink_secondary_btn {{
        font-family: {FONT_DISPLAY};
        font-size: 13px;
        font-weight: 600;
        color: {T["accent_bark"]};
        background: {T["bg_elevated"]};
        border: 2px solid {T["accent_bark"]};
        border-radius: 14px;
        padding: 5px 16px;
        min-height: 26px;
        outline: none;
    }}
    QPushButton#drink_secondary_btn:hover {{
        background: #fdf3d4;
        color: {T["accent_bark_hi"]};
        border: 2px solid {T["accent_bark_hi"]};
    }}
    QPushButton#drink_secondary_btn:pressed {{
        background: #f4e6b8;
        color: {T["accent_bark"]};
        border: 2px solid #6b4f36;
    }}
    QPushButton#drink_secondary_btn:disabled {{
        background: {T["bg_surface"]};
        color: {T["text_muted"]};
        border: 2px solid #d8cca0;
    }}
    QPushButton#drink_secondary_btn:focus {{
        border: 2px solid {T["accent_leaf"]};
    }}

    /* 辅按钮：透明底，只在 hover 时出淡底 */
    QPushButton#drink_tertiary_btn {{
        font-family: {FONT_DISPLAY};
        font-size: 13px;
        font-weight: 500;
        color: {T["text_secondary"]};
        background: transparent;
        border: none;
        border-radius: 14px;
        padding: 5px 14px;
        min-height: 26px;
        outline: none;
    }}
    QPushButton#drink_tertiary_btn:hover {{
        background: rgba(139, 106, 74, 0.08);
        color: {T["text_primary"]};
    }}
    QPushButton#drink_tertiary_btn:pressed {{
        background: rgba(139, 106, 74, 0.15);
        color: {T["text_primary"]};
    }}
    QPushButton#drink_tertiary_btn:disabled {{
        background: transparent;
        color: {T["text_muted"]};
    }}
    QPushButton#drink_tertiary_btn:focus {{
        border: 2px solid {T["accent_leaf"]};
    }}

    /* 齿轮 QToolButton */
    QToolButton#settings_icon_btn {{
        background: transparent;
        border: none;
        border-radius: 14px;
        padding: 2px;
    }}
    QToolButton#settings_icon_btn:hover {{
        background: {T["bg_surface"]};
    }}
    QToolButton#settings_icon_btn:pressed {{
        background: {T["border_subtle"]};
    }}
    QToolButton#settings_icon_btn:focus {{
        border: 2px solid {T["accent_leaf"]};
    }}

    /* 圆点小标记 */
    QLabel#status_dot_default {{
        background: {T["success_water"]};
        border-radius: 3px;
        min-width: 6px; max-width: 6px;
        min-height: 6px; max-height: 6px;
    }}
    QLabel#status_dot_pending {{
        background: {T["warning_dry"]};
        border-radius: 3px;
        min-width: 6px; max-width: 6px;
        min-height: 6px; max-height: 6px;
    }}
    QLabel#status_dot_snooze {{
        background: {T["accent_bark"]};
        border-radius: 3px;
        min-width: 6px; max-width: 6px;
        min-height: 6px; max-height: 6px;
    }}

    /* Toast */
    QLabel#first_launch_toast {{
        font-family: {FONT_BODY};
        font-size: 12px;
        font-weight: 500;
        color: {T["text_primary"]};
        background: {T["bg_elevated"]};
        border: 2px solid {T["accent_leaf"]};
        border-radius: 20px;
        padding: 8px 18px;
    }}
    """


# ----------------------------- 原子写盘 -----------------------------


def atomic_write_json(path: Path, data) -> None:
    """写临时文件 + Path.replace 原子 rename，防中途崩溃留残缺文件。

    不使用 default=str 兜底：dataclass 之外的不可序列化对象（set、datetime）
    如果混进来，宁可抛 TypeError 让上层显式处理，也不静默转成 str 字面量
    污染数据。业务层的 datetime 一律 .isoformat() 后再入库。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_json_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return raw if isinstance(raw, list) else []


def _filter_known_fields(cls, raw: dict) -> dict:
    """dataclass 构造前滤掉未知字段。旧 schema 文件里已删除的字段（如
    expected_drink_ml / final_wilt_level）直接丢弃，防 TypeError。"""
    known = {f.name for f in cls.__dataclass_fields__.values()}
    return {k: v for k, v in raw.items() if k in known}


# ----------------------------- 数据对象 -----------------------------


@dataclass
class TodayState:
    date: str = ""
    drunk_ml: int = 0
    last_reminder_ts: Optional[str] = None
    # 当前是否有一次"未处理"的提醒在等用户响应。
    # True：可以记一次喝水（弹窗里点已喝、或主窗口点我喝水了），任何一种都只生效一次
    # False：不在提醒窗口期，主窗口按钮 disabled
    reminder_pending: bool = False
    # v2 新增
    daily_goal_ml_snapshot: int = 2000
    reminder_count: int = 0
    drank_count: int = 0
    session_started_at: Optional[str] = None  # 当天首次提醒时间戳（跨日归档用）
    # v3 新增
    skip_count: int = 0  # 连续跳过次数（0/1/2），已喝归零，到 SKIP_RESET_THRESHOLD 触发视觉重置
    visual_reset_ml: int = 0  # 视觉基线：stage 从 (drunk_ml - visual_reset_ml) / goal 算
    # v4 新增：待兑现的再提醒（推迟/不理产生）。存墙上时钟到期时间并落盘，
    # app 重启 / 系统睡眠唤醒后由 _tick 兑现，不依赖 QTimer 间隔
    followup_due_ts: Optional[str] = None
    followup_source: Optional[str] = None  # snoozed_followup / ignored_followup


@dataclass
class Reminder:
    """一次定时触发的喝水提醒。structure 对象 1 的 C 属性表落地。"""

    id: str
    triggered_at: str  # ISO datetime
    source: str  # scheduled / snoozed_followup / ignored_followup / catch_up
    response: Optional[str] = None  # drank / skipped / snoozed / ignored
    responded_at: Optional[str] = None
    actual_drunk_ml: Optional[int] = None
    linked_drink_entry_id: Optional[str] = None


@dataclass
class DrinkEntry:
    """一次确认的喝水事件。structure 对象 4 的 C 属性表落地。"""

    id: str
    timestamp: str  # ISO datetime
    amount_ml: int
    source: str  # response / supplement / proactive
    day_date: str  # YYYY-MM-DD，归属哪一天
    linked_reminder_id: Optional[str] = None


@dataclass
class HistoryEntry:
    """一天的凝结快照。structure 对象 6 的 C 属性表落地。跨日归档时创建。"""

    date: str  # YYYY-MM-DD，主键
    drunk_ml_total: int
    daily_goal_ml_snapshot: int
    is_goal_reached: bool
    final_growth_stage: int
    reminder_count: int
    response_breakdown: dict  # {drank: N, skipped: N, snoozed: N, ignored: N}
    drink_entry_ids: list  # 当天 DrinkEntry id 列表
    archived_at: str  # ISO datetime
    session_started_at: Optional[str] = None


# ----------------------------- 配置 -----------------------------


@dataclass
class Config:
    interval_min: int = 60
    per_cup_ml: int = 250
    daily_goal_ml: int = 2000
    launch_on_startup: bool = False
    # v4 新增：免打扰时段（睡觉时间不提醒），HH:MM，支持跨午夜
    quiet_enabled: bool = True
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"
    schema_version: int = SCHEMA_VERSION
    today: TodayState = field(default_factory=TodayState)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.ensure_today()
            cfg.save()
            return cfg
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        today_raw = raw.get("today", {}) or {}
        # 兼容旧字段 last_reminder_confirmed（语义反向）
        if "last_reminder_confirmed" in today_raw:
            old = today_raw.pop("last_reminder_confirmed")
            today_raw.setdefault("reminder_pending", not old)

        # schema 迁移：TodayState 补新字段默认值（v1→v2、v2→v3 共用同一段 setdefault）
        raw_schema = raw.get("schema_version", 1)
        needs_migration = raw_schema < SCHEMA_VERSION
        if needs_migration:
            # v1 → v2 字段
            today_raw.setdefault(
                "daily_goal_ml_snapshot",
                raw.get("daily_goal_ml", 2000),
            )
            today_raw.setdefault("reminder_count", 0)
            today_raw.setdefault("drank_count", 0)
            today_raw.setdefault("session_started_at", None)
            # v2 → v3 字段
            today_raw.setdefault("skip_count", 0)
            today_raw.setdefault("visual_reset_ml", 0)
            # v3 → v4：wilt_level 删除（由下方未知字段过滤自动丢弃），
            # followup_* / quiet_* 新字段靠 dataclass 默认值补齐，无需 setdefault

        # 过滤掉未知字段，防 TodayState(**today_raw) 抛 TypeError
        known_fields = {f.name for f in TodayState.__dataclass_fields__.values()}
        today_raw = {k: v for k, v in today_raw.items() if k in known_fields}

        cfg = cls(
            interval_min=raw.get("interval_min", 60),
            per_cup_ml=raw.get("per_cup_ml", 250),
            daily_goal_ml=raw.get("daily_goal_ml", 2000),
            launch_on_startup=raw.get("launch_on_startup", False),
            quiet_enabled=raw.get("quiet_enabled", True),
            quiet_start=raw.get("quiet_start", "23:00"),
            quiet_end=raw.get("quiet_end", "08:00"),
            schema_version=SCHEMA_VERSION,
            today=TodayState(**today_raw) if today_raw else TodayState(),
        )
        cfg.ensure_today()
        if needs_migration:
            # 迁移后立即落盘一次，触发 schema_version=2 持久化
            cfg.save()
        return cfg

    def save(self) -> None:
        atomic_write_json(CONFIG_PATH, asdict(self))

    def ensure_today(self) -> bool:
        """仅兜底首次启动 / today.date 为空的情况，返回是否发生了初始化。

        v2 行为契约：
        - today.date 为空（首次启动 / 从未运行过）→ 用今天初始化 fresh 并 save，
          返回 True。
        - today.date 非空但跟今天不匹配（跨日了）→ **不动 today、不 save**，
          直接返回 False。跨日归档统一由 MainWindow.check_day_rollover 走
          TransactionGuard 流程处理（先写 HistoryEntry 成功后才重置），
          避免这里抢先 fresh 掉旧 today.date 导致归档链路失效。
        - today.date == 今天 → 无操作，返回 False。
        """
        today_str = date.today().isoformat()
        if not self.today.date:
            self.today = TodayState(
                date=today_str,
                daily_goal_ml_snapshot=self.daily_goal_ml,
            )
            self.save()
            return True
        return False

    @property
    def progress(self) -> float:
        if self.daily_goal_ml <= 0:
            return 0.0
        return min(1.0, self.today.drunk_ml / self.daily_goal_ml)


# ----------------------------- Repository -----------------------------


class RemindersStore:
    """Reminder 持久化。JSON 数组文件，每条 asdict(reminder)。"""

    def __init__(self, path: Path = REMINDERS_PATH) -> None:
        self.path = path

    def load(self) -> list[Reminder]:
        return [
            Reminder(**_filter_known_fields(Reminder, r))
            for r in _read_json_list(self.path)
        ]

    def _save_all(self, items: list[Reminder]) -> None:
        atomic_write_json(self.path, [asdict(r) for r in items])

    def append(self, reminder: Reminder) -> None:
        items = self.load()
        items.append(reminder)
        self._save_all(items)

    def update(self, reminder: Reminder) -> None:
        items = self.load()
        for i, r in enumerate(items):
            if r.id == reminder.id:
                items[i] = reminder
                break
        else:
            items.append(reminder)
        self._save_all(items)


class DrinkEntriesStore:
    """DrinkEntry 持久化。"""

    def __init__(self, path: Path = DRINK_ENTRIES_PATH) -> None:
        self.path = path

    def load(self) -> list[DrinkEntry]:
        return [
            DrinkEntry(**_filter_known_fields(DrinkEntry, e))
            for e in _read_json_list(self.path)
        ]

    def _save_all(self, items: list[DrinkEntry]) -> None:
        atomic_write_json(self.path, [asdict(e) for e in items])

    def append(self, entry: DrinkEntry) -> None:
        items = self.load()
        items.append(entry)
        self._save_all(items)


class HistoryStore:
    """HistoryEntry 持久化。跨日归档写入。"""

    def __init__(self, path: Path = HISTORY_PATH) -> None:
        self.path = path

    def load(self) -> list[HistoryEntry]:
        return [
            HistoryEntry(**_filter_known_fields(HistoryEntry, h))
            for h in _read_json_list(self.path)
        ]

    def _save_all(self, items: list[HistoryEntry]) -> None:
        atomic_write_json(self.path, [asdict(h) for h in items])

    def append(self, entry: HistoryEntry) -> None:
        items = self.load()
        # 幂等：同一 date 只保留一条（后写覆盖前写，防 rollover 补跑重复）
        items = [h for h in items if h.date != entry.date]
        items.append(entry)
        self._save_all(items)


# ----------------------------- 画布 -----------------------------


def _tree_stage(progress: float) -> int:
    """9 档均匀分布（每 11.1%），返回 0-8。
    0-10% → 0（种子），11-21% → 1（嫩芽），... 88-99% → 8（大树），100%+ → 显示果实由调用方处理。

    progress 传入的应该是"视觉 progress"，即 (drunk_ml - visual_reset_ml) / daily_goal_ml，
    可能为负数（跳过 3 次重置后但 drunk_ml 尚未追上 baseline）— 负数按 0 处理。
    """
    if progress <= 0:
        return 0
    return min(int(progress * STAGE_COUNT), STAGE_COUNT - 1)


class TreeCanvas(QWidget):
    """主画布：水彩手绘风资源图渲染。根据 Config 的当日状态自动选图。

    渲染规则：
    - 健康图按 stage 选（stage 3/4 复用大树图，无中间状态资源）
    - wilt > 0 时，在健康图上叠加沙漠图，opacity = min(wilt/3, 1.0)
    - 图按 KeepAspectRatio 缩放后居中绘制，两侧留米色底
    """

    def __init__(self, cfg: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setMinimumSize(QSize(380, 320))
        self._images: dict[str, Optional[QPixmap]] = {}

    def _get_image(self, name: str) -> Optional[QPixmap]:
        if name not in self._images:
            path = ASSETS_DIR / name
            pix = QPixmap(str(path)) if path.exists() else QPixmap()
            self._images[name] = pix if not pix.isNull() else None
        return self._images.get(name)

    def _draw_cover(self, painter: QPainter, pix: QPixmap, rect, opacity: float) -> None:
        if pix is None or pix.isNull():
            return
        scaled = pix.scaled(
            rect.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (rect.width() - scaled.width()) // 2
        y = (rect.height() - scaled.height()) // 2
        painter.setOpacity(opacity)
        painter.drawPixmap(x, y, scaled)
        painter.setOpacity(1.0)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()

        # 米色底：图按 KeepAspectRatio 缩放后两侧会留白，用图边缘色填充
        p.fillRect(rect, QColor(CANVAS_BG_COLOR))

        today = self.cfg.today
        goal = self.cfg.daily_goal_ml

        # 果实优先：达成今日目标 → 直接显示 stage9_s0 果实图，不受 skip / reset 影响
        if goal > 0 and today.drunk_ml >= goal:
            asset_name = FRUIT_ASSET
        else:
            # 视觉 progress = (drunk_ml - visual_reset_ml) / goal，可能为负（重置刚触发）
            visual_progress = (today.drunk_ml - today.visual_reset_ml) / goal if goal > 0 else 0.0
            stage = _tree_stage(visual_progress)
            skip = min(max(today.skip_count, 0), 2)  # clamp 到 0/1/2
            asset_name = STAGE_ASSETS[stage][skip]

        img = self._get_image(asset_name)
        if img is not None:
            self._draw_cover(p, img, rect, opacity=1.0)
        else:
            p.setPen(QColor("#888"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"缺资源: {asset_name}")


# ----------------------------- 弹窗 -----------------------------


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.cfg = cfg

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(15, 180)
        self.interval_spin.setSingleStep(5)
        self.interval_spin.setSuffix(" 分钟")
        self.interval_spin.setValue(cfg.interval_min)
        form.addRow("提醒间隔", self.interval_spin)

        self.cup_spin = QSpinBox()
        self.cup_spin.setRange(50, 1000)
        self.cup_spin.setSingleStep(50)
        self.cup_spin.setSuffix(" ml")
        self.cup_spin.setValue(cfg.per_cup_ml)
        form.addRow("一杯水", self.cup_spin)

        self.goal_spin = QSpinBox()
        self.goal_spin.setRange(500, 5000)
        self.goal_spin.setSingleStep(100)
        self.goal_spin.setSuffix(" ml")
        self.goal_spin.setValue(cfg.daily_goal_ml)
        form.addRow("每日目标", self.goal_spin)

        self.quiet_checkbox = QCheckBox("夜间免打扰（此时段不弹提醒）")
        self.quiet_checkbox.setChecked(cfg.quiet_enabled)
        form.addRow("", self.quiet_checkbox)

        self.quiet_start_edit = QTimeEdit()
        self.quiet_start_edit.setDisplayFormat("HH:mm")
        self.quiet_start_edit.setTime(QTime.fromString(cfg.quiet_start, "HH:mm"))
        form.addRow("免打扰开始", self.quiet_start_edit)

        self.quiet_end_edit = QTimeEdit()
        self.quiet_end_edit.setDisplayFormat("HH:mm")
        self.quiet_end_edit.setTime(QTime.fromString(cfg.quiet_end, "HH:mm"))
        form.addRow("免打扰结束", self.quiet_end_edit)

        self.startup_checkbox = QCheckBox("开机自启动")
        self.startup_checkbox.setChecked(cfg.launch_on_startup)
        form.addRow("", self.startup_checkbox)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply_to(self, cfg: Config) -> None:
        cfg.interval_min = self.interval_spin.value()
        cfg.per_cup_ml = self.cup_spin.value()
        cfg.daily_goal_ml = self.goal_spin.value()
        cfg.quiet_enabled = self.quiet_checkbox.isChecked()
        cfg.quiet_start = self.quiet_start_edit.time().toString("HH:mm")
        cfg.quiet_end = self.quiet_end_edit.time().toString("HH:mm")
        want_startup = self.startup_checkbox.isChecked()
        if want_startup != cfg.launch_on_startup:
            # 同步启动文件夹：成功才更新 cfg 字段（避免 config 说开着但文件系统不一致）
            if sync_startup_shortcut(want_startup):
                cfg.launch_on_startup = want_startup
        cfg.save()


# ----------------------------- 主窗口 -----------------------------


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("喝水小助手")
        self.setMinimumSize(420, 640)  # 1:1 图 + 4 行按钮，约 640px 足够
        self.setWindowIcon(make_app_icon())
        self.setStyleSheet(build_theme_qss())

        # Windows pythonw 启动时主窗口可能被扔到 (-25600, -25600) 屏幕外，
        # 强制移到主屏幕中心稍偏上，防止用户开机看不到 app
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = avail.x() + (avail.width() - 420) // 2
            y = avail.y() + max(60, (avail.height() - 640) // 3)
            self.move(x, y)

        # v2 数据仓库
        self.reminders_store = RemindersStore()
        self.drink_entries_store = DrinkEntriesStore()
        self.history_store = HistoryStore()

        # 当前 pending 的 Reminder 引用（响应完清空）
        self._current_reminder: Optional[Reminder] = None
        # 跨日归档挂起标记（dialog 已删除后仅剩 SettingsDialog 场景触发）
        self._dialog_visible = False
        self._pending_rollover = False
        # 当前 pending 提醒的响应截止时间（墙上时钟，超时视为不理）
        self._pending_deadline: Optional[datetime] = None
        # 下次定时提醒的到期时间（墙上时钟）。None = 周期停止（达标后）
        self._next_reminder_due: Optional[datetime] = None
        # 人不在电脑前的起点（无输入超 IDLE_AWAY_MIN 分钟时设置）
        self._away_since: Optional[datetime] = None
        # 最近一次喝水记录时间（主动记水冷却用），启动时从 drink_entries 恢复，
        # 防止重启绕过冷却
        self._last_drink_ts: Optional[datetime] = None
        _entries = self.drink_entries_store.load()
        if _entries:
            try:
                self._last_drink_ts = datetime.fromisoformat(_entries[-1].timestamp)
            except (ValueError, TypeError):
                self._last_drink_ts = None
        # 记录切换动画对象（避免被 GC）
        self._layout_anim: Optional[QPropertyAnimation] = None
        # 上一次布局是否是提醒态（判断是否需要触发切换动画）
        self._last_layout_was_reminder = False
        # 首次启动标记：由 main() 在 Config.load 前探测文件存在与否后设置
        self._first_launch = False

        # 托盘 flash 状态
        self._tray_icon_ref: Optional[QSystemTrayIcon] = None
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(500)
        self._flash_timer.timeout.connect(self._on_flash_tick)
        self._flash_count = 0

        # ---------- 布局 ----------
        central = QWidget()
        central.setObjectName("central_widget")
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        # 顶部行：标题 + 齿轮
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.title_label = QLabel()
        self.title_label.setObjectName("title_label")
        top_row.addWidget(self.title_label, stretch=1)

        self.settings_icon_btn = QToolButton()
        self.settings_icon_btn.setObjectName("settings_icon_btn")
        self.settings_icon_btn.setIcon(make_gear_icon())
        self.settings_icon_btn.setIconSize(QSize(20, 20))
        self.settings_icon_btn.setFixedSize(28, 28)
        self.settings_icon_btn.setToolTip("设置")
        self.settings_icon_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_icon_btn.clicked.connect(self.open_settings)
        top_row.addWidget(self.settings_icon_btn, alignment=Qt.AlignmentFlag.AlignTop)

        root.addLayout(top_row)

        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("subtitle_label")
        root.addWidget(self.subtitle_label)

        # 倒计时行：圆点 + 文字
        countdown_row = QHBoxLayout()
        countdown_row.setSpacing(6)
        self.status_dot = QLabel()
        self.status_dot.setObjectName("status_dot_default")
        countdown_row.addWidget(self.status_dot, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.countdown_label = QLabel()
        self.countdown_label.setObjectName("countdown_label")
        countdown_row.addWidget(self.countdown_label)
        countdown_row.addStretch()
        root.addLayout(countdown_row)

        # 画布（主视觉）
        self.canvas = TreeCanvas(cfg)
        root.addWidget(self.canvas, stretch=1)

        # Toast 挂载容器（占位一层 padding，Toast 用 overlay 在 central 内绝对定位）
        # Toast 位置：画布下方，按钮行上方
        self._toast: Optional[FirstLaunchToast] = None

        # 行动区：常态 + 提醒态两组子控件，通过 setVisible 切换
        self.action_zone = QWidget()
        self.action_zone.setObjectName("action_zone")
        self.action_zone.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        az_layout = QVBoxLayout(self.action_zone)
        az_layout.setContentsMargins(16, 16, 16, 16)
        az_layout.setSpacing(8)

        # ---- 常态区（proactive）跟提醒态一样的 3 快捷 ----
        self.normal_zone = QWidget()
        self.normal_zone.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        nz_layout = QVBoxLayout(self.normal_zone)
        nz_layout.setContentsMargins(0, 0, 0, 0)
        nz_layout.setSpacing(8)

        per = cfg.per_cup_ml
        # 快捷主行：一口 + 半杯
        n_quick_top = QHBoxLayout()
        n_quick_top.setSpacing(8)
        self.n_gulp_btn = QPushButton("主动喝一口 +50ml")
        self.n_gulp_btn.setObjectName("drink_secondary_btn")
        self.n_gulp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.n_gulp_btn.setAutoDefault(False)
        self.n_gulp_btn.clicked.connect(self._on_proactive_gulp)
        n_quick_top.addWidget(self.n_gulp_btn, stretch=1)

        self.n_half_cup_btn = QPushButton(f"主动喝半杯 +{per // 2}ml")
        self.n_half_cup_btn.setObjectName("drink_secondary_btn")
        self.n_half_cup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.n_half_cup_btn.setAutoDefault(False)
        self.n_half_cup_btn.clicked.connect(self._on_proactive_half_cup)
        n_quick_top.addWidget(self.n_half_cup_btn, stretch=1)
        nz_layout.addLayout(n_quick_top)

        # 主按钮：喝一杯
        self.n_full_cup_btn = QPushButton(f"主动喝一杯 +{per}ml")
        self.n_full_cup_btn.setObjectName("drink_primary_btn")
        self.n_full_cup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.n_full_cup_btn.setAutoDefault(False)
        self.n_full_cup_btn.clicked.connect(self._on_proactive_full_cup)
        nz_layout.addWidget(self.n_full_cup_btn)

        az_layout.addWidget(self.normal_zone)

        # ---- 提醒响应区（reminder response）----
        self.reminder_zone = QWidget()
        self.reminder_zone.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rz_layout = QVBoxLayout(self.reminder_zone)
        rz_layout.setContentsMargins(0, 0, 0, 0)
        rz_layout.setSpacing(6)

        # 提示语行（去掉 SpinBox，量在 Settings 里改 per_cup_ml）
        self.reminder_hint_label = QLabel("起来动一动，喝口水吧")
        self.reminder_hint_label.setObjectName("reminder_hint_label")
        rz_layout.addWidget(self.reminder_hint_label)

        per = cfg.per_cup_ml
        # 快捷主行：一口（50 固定）+ 半杯（spin/2）
        quick_top_row = QHBoxLayout()
        quick_top_row.setSpacing(8)
        self.gulp_btn = QPushButton("喝一口 +50ml")
        self.gulp_btn.setObjectName("drink_secondary_btn")
        self.gulp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.gulp_btn.setAutoDefault(False)
        self.gulp_btn.clicked.connect(self._on_reminder_gulp)
        quick_top_row.addWidget(self.gulp_btn, stretch=1)

        self.half_cup_btn = QPushButton(f"喝半杯 +{per // 2}ml")
        self.half_cup_btn.setObjectName("drink_secondary_btn")
        self.half_cup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.half_cup_btn.setAutoDefault(False)
        self.half_cup_btn.clicked.connect(self._on_reminder_half_cup)
        quick_top_row.addWidget(self.half_cup_btn, stretch=1)
        rz_layout.addLayout(quick_top_row)

        # 主按钮：喝一杯（当前 SpinBox 值，主色苔绿）
        self.full_cup_btn = QPushButton(f"喝一杯 +{per}ml")
        self.full_cup_btn.setObjectName("drink_primary_btn")
        self.full_cup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.full_cup_btn.setAutoDefault(False)
        self.full_cup_btn.clicked.connect(self._on_reminder_full_cup)
        rz_layout.addWidget(self.full_cup_btn)

        # 底行：推迟 + 跳过
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.snooze_btn = QPushButton("推迟 5 分")
        self.snooze_btn.setObjectName("drink_secondary_btn")
        self.snooze_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.snooze_btn.setAutoDefault(False)
        self.snooze_btn.clicked.connect(self._on_reminder_snooze)
        btn_row.addWidget(self.snooze_btn, stretch=1)

        self.skip_btn = QPushButton("跳过")
        self.skip_btn.setObjectName("drink_tertiary_btn")
        self.skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_btn.setAutoDefault(False)
        self.skip_btn.clicked.connect(self._on_reminder_skip)
        btn_row.addWidget(self.skip_btn, stretch=1)

        rz_layout.addLayout(btn_row)

        az_layout.addWidget(self.reminder_zone)

        # 行动区淡入淡出用的透明度 effect（常驻，避免 GC）
        self._action_effect = QGraphicsOpacityEffect(self.action_zone)
        self._action_effect.setOpacity(1.0)
        self.action_zone.setGraphicsEffect(self._action_effect)

        root.addWidget(self.action_zone)

        self.setCentralWidget(central)

        # 跨日切换检查
        self.day_check_timer = QTimer(self)
        self.day_check_timer.timeout.connect(self.check_day_rollover)
        self.day_check_timer.start(60 * 1000)  # 每分钟检查

        # 每秒 tick：兑现到期事件 + 刷倒计时
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self._tick)
        self.countdown_timer.start(1000)

        # 30 秒 raise singleShot 引用（记着可取消）
        self._raise_timer: Optional[QTimer] = None

        # startup 归档补跑
        self.check_day_rollover()

        self.refresh()
        self.start_reminder_cycle()

    def attach_tray(self, tray: QSystemTrayIcon) -> None:
        """setup_tray 后回传 tray 引用，供 flash 使用。"""
        self._tray_icon_ref = tray

    # --- UI ---

    def refresh(self) -> None:
        t = self.cfg.today
        self.title_label.setText(
            f"今日 {t.drunk_ml} / {self.cfg.daily_goal_ml} ml"
        )
        progress_pct = int(self.cfg.progress * 100)
        # v3：状态文字读 skip_count；果实态和视觉重置态有独立文案
        goal = self.cfg.daily_goal_ml
        if goal > 0 and t.drunk_ml >= goal:
            state_text = "今日达成 · 果实累累"
        elif t.visual_reset_ml > 0 and t.drunk_ml <= t.visual_reset_ml:
            state_text = "重新开始 · 种子"
        else:
            state_text = {
                0: "水灵",
                1: "有点蔫",
                2: "枯黄",
            }.get(min(t.skip_count, 2), "枯黄")
        self.subtitle_label.setText(f"完成 {progress_pct}%  ·  {state_text}")
        self.canvas.update()

        # 常态 3 快捷按钮文案（半杯/一杯跟 per_cup_ml 动态）
        per = self.cfg.per_cup_ml
        self.n_half_cup_btn.setText(f"主动喝半杯 +{per // 2}ml")
        self.n_full_cup_btn.setText(f"主动喝一杯 +{per}ml")

        # 布局态切换
        pending = t.reminder_pending
        show_reminder_layout = pending
        # 提醒态子区
        if show_reminder_layout:
            self.normal_zone.setVisible(False)
            self.reminder_zone.setVisible(True)
            # followup 提醒是最后一次机会，提示语说明利害（catch_up 不算）
            is_second_chance = (
                self._current_reminder is not None
                and self._current_reminder.source in ("snoozed_followup", "ignored_followup")
            )
            self.reminder_hint_label.setText(
                "再不喝小树要蔫了，就现在吧" if is_second_chance else "起来动一动，喝口水吧"
            )
            # 提醒态半杯/一杯按钮文案跟随 per_cup_ml（同步常态那两个）
            self.half_cup_btn.setText(f"喝半杯 +{per // 2}ml")
            self.full_cup_btn.setText(f"喝一杯 +{per}ml")
        else:
            self.reminder_zone.setVisible(False)
            self.normal_zone.setVisible(True)

        # 布局切换动画（态变化时触发 fade）
        if show_reminder_layout != self._last_layout_was_reminder:
            self._play_fade_in()
            self._last_layout_was_reminder = show_reminder_layout

        # 圆点色跟状态挂钩
        if self.cfg.today.followup_due_ts is not None:
            self.status_dot.setObjectName("status_dot_snooze")
        elif pending:
            self.status_dot.setObjectName("status_dot_pending")
        else:
            self.status_dot.setObjectName("status_dot_default")
        # objectName 变了要重新应用样式表
        self.status_dot.setStyleSheet(self.status_dot.styleSheet())
        # 强制重新计算 QSS
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)

        # 倒计时立即刷
        self._update_countdown()

    def _play_fade_in(self) -> None:
        """行动区淡入。切换布局态时用。"""
        if not THEME["motion_enabled"]:
            self._action_effect.setOpacity(1.0)
            return
        anim = QPropertyAnimation(self._action_effect, b"opacity", self)
        anim.setDuration(THEME["dur_normal"])
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutQuart)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._layout_anim = anim

    def _tick(self) -> None:
        """每秒 tick：先兑现墙上时钟的到期事件，再刷倒计时文案。
        用到期时间而不是 QTimer 间隔计时，系统睡眠唤醒 / app 重启后照样兑现。"""
        now = datetime.now()
        t = self.cfg.today

        # 人不在检测：无键鼠输入超过 IDLE_AWAY_MIN 分钟（锁屏/熄屏/离开都算）
        # → 挂起一切到期事件和惩罚。时间到了也记不到，不该弹也不该罚
        idle_sec = get_idle_seconds()
        if idle_sec >= IDLE_AWAY_MIN * 60:
            if self._away_since is None:
                self._away_since = now - timedelta(seconds=idle_sec)
            self._update_countdown()
            return
        if self._away_since is not None:
            away_sec = (now - self._away_since).total_seconds()
            self._away_since = None
            self._on_return_from_away(away_sec)

        # followup 到期 → 再提醒（推迟 5 分钟 / 不理后的第二次机会）
        if t.followup_due_ts and not t.reminder_pending:
            due = self._parse_ts(t.followup_due_ts)
            if due is None:
                self._clear_followup()
            elif now >= due:
                src = t.followup_source or "snoozed_followup"
                self._clear_followup()
                self.show_reminder(source=src)

        # 提醒弹出后超时无响应 → 按不理处理
        if (
            t.reminder_pending
            and self._pending_deadline is not None
            and now >= self._pending_deadline
        ):
            self._on_response_timeout()

        # 定时提醒到期（墙上时钟，替代旧 QTimer 间隔计时）
        if (
            not t.reminder_pending
            and self._next_reminder_due is not None
            and now >= self._next_reminder_due
        ):
            self.show_reminder(source="scheduled")

        self._update_countdown()

    def _on_return_from_away(self, away_sec: float) -> None:
        """人回到电脑前。离开够久 → 旧提醒作废（不惩罚，人不在不等于不理）+
        补弹一次；短暂离开 → pending 的响应窗口重新给满。"""
        if away_sec >= WAKE_CATCHUP_MIN * 60:
            if self._current_reminder is not None and self._current_reminder.response is None:
                self._current_reminder.response = "ignored"
                self._current_reminder.responded_at = datetime.now().isoformat()
                self.reminders_store.update(self._current_reminder)
                self._current_reminder = None
            self._pending_deadline = None
            if self.cfg.today.reminder_pending:
                self.cfg.today.reminder_pending = False
                self.cfg.save()
            self._clear_followup()
            self.show_reminder(source="catch_up")
        elif self.cfg.today.reminder_pending:
            self._pending_deadline = datetime.now() + timedelta(minutes=RESPONSE_WINDOW_MIN)

    @staticmethod
    def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None

    def _update_countdown(self) -> None:
        """刷新倒计时文案。状态优先级：达标 > pending > followup > 免打扰 > 主 timer。"""
        goal = self.cfg.daily_goal_ml
        if goal > 0 and self.cfg.today.drunk_ml >= goal:
            self.countdown_label.setText("今日已达成 · 明天见")
            return

        if self.cfg.today.reminder_pending:
            self.countdown_label.setText("该喝水了 · 未响应")
            return

        due = self._parse_ts(self.cfg.today.followup_due_ts)
        if due is not None:
            remaining = int((due - datetime.now()).total_seconds())
            if remaining > 0:
                self.countdown_label.setText(
                    f"{remaining // 60:02d}:{remaining % 60:02d} 后再提醒"
                )
                return

        if self._in_quiet_hours():
            self.countdown_label.setText(f"免打扰中 · {self.cfg.quiet_end} 后恢复")
            return

        if self._next_reminder_due is None:
            self.countdown_label.setText("等待中…")
            return
        total_seconds = int((self._next_reminder_due - datetime.now()).total_seconds())
        if total_seconds <= 0:
            self.countdown_label.setText("即将提醒…")
            return
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        self.countdown_label.setText(f"距离下次提醒：{minutes:02d}:{seconds:02d}")

    # --- 计时 ---

    def start_reminder_cycle(self) -> None:
        self._next_reminder_due = datetime.now() + timedelta(minutes=self.cfg.interval_min)

    def restart_reminder_cycle(self) -> None:
        self.start_reminder_cycle()

    def stop_reminder_cycle(self) -> None:
        self._next_reminder_due = None

    # --- 状态机辅助 ---

    def _schedule_followup(self, source: str) -> None:
        """FOLLOW_UP_MIN 分钟后再提醒。存墙上时钟到期时间并落盘，由 _tick 兑现。"""
        due = datetime.now() + timedelta(minutes=FOLLOW_UP_MIN)
        self.cfg.today.followup_due_ts = due.isoformat()
        self.cfg.today.followup_source = source
        self.cfg.save()

    def _clear_followup(self) -> None:
        if self.cfg.today.followup_due_ts or self.cfg.today.followup_source:
            self.cfg.today.followup_due_ts = None
            self.cfg.today.followup_source = None
            self.cfg.save()

    def _apply_skip_penalty(self) -> None:
        """跳过 / 两次不理的惩罚：skip_count +1，到阈值视觉重置回种子（drunk_ml 保留）。"""
        self.cfg.today.skip_count += 1
        if self.cfg.today.skip_count >= SKIP_RESET_THRESHOLD:
            self.cfg.today.visual_reset_ml = self.cfg.today.drunk_ml
            self.cfg.today.skip_count = 0

    def _in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        """当前是否在免打扰时段。支持跨午夜（如 23:00-08:00）。"""
        if not self.cfg.quiet_enabled:
            return False
        try:
            sh, sm = (int(x) for x in self.cfg.quiet_start.split(":"))
            eh, em = (int(x) for x in self.cfg.quiet_end.split(":"))
        except (ValueError, AttributeError):
            return False
        start, end = sh * 60 + sm, eh * 60 + em
        if start == end:
            return False
        moment = now or datetime.now()
        cur = moment.hour * 60 + moment.minute
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end

    def _proactive_cooldown_remaining_min(self) -> int:
        """主动记水冷却剩余分钟数（向上取整），0 = 可以记。"""
        if self._last_drink_ts is None:
            return 0
        elapsed = (datetime.now() - self._last_drink_ts).total_seconds()
        remain = PROACTIVE_COOLDOWN_MIN * 60 - elapsed
        if remain <= 0:
            return 0
        return int(remain // 60) + 1

    def _show_cooldown_feedback(self, remain_min: int) -> None:
        """主动记水被冷却拦下时的反馈，不允许静默失败。"""
        text = f"刚记过一次啦，{remain_min} 分钟后可以再记"
        if self.isVisible() and not self.isMinimized():
            toast = FirstLaunchToast(text, parent=self.centralWidget())
            self._toast = toast
            toast.show_at_canvas_bottom(self.canvas, self.action_zone)
        elif self._tray_icon_ref is not None:
            self._tray_icon_ref.showMessage(
                "喝水小助手",
                text,
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def show_reminder(self, source: str = "scheduled") -> None:
        """内嵌响应模式。主窗口本身承担响应界面。"""
        # 达标后不再提醒（followup 也吞掉），也不重启周期
        goal = self.cfg.daily_goal_ml
        if goal > 0 and self.cfg.today.drunk_ml >= goal:
            self._clear_followup()
            return

        # 免打扰时段不弹。scheduled 重排下一轮；followup 直接作废（人在睡觉）
        if self._in_quiet_hours():
            self._clear_followup()
            if source == "scheduled":
                self.restart_reminder_cycle()
            return

        # Debounce：防 QTimer 在系统休眠/时钟跳动后连触发（0.4 秒内 3 次的 bug）。
        # 只对 scheduled 有效，followup 走墙上时钟到期兑现不受影响。
        if source == "scheduled" and self.cfg.today.last_reminder_ts:
            try:
                last = datetime.fromisoformat(self.cfg.today.last_reminder_ts)
                min_gap_sec = self.cfg.interval_min * 60 * 0.9  # 允许 10% 提前误差
                elapsed = (datetime.now() - last).total_seconds()
                if elapsed < min_gap_sec:
                    # 距离上次触发太近，跳过 + restart 让下次按新时间点到
                    self.restart_reminder_cycle()
                    return
            except (ValueError, TypeError):
                pass

        # 上次还挂着未响应的 reminder → 抢占标 ignored
        if self._current_reminder is not None and self._current_reminder.response is None:
            self._current_reminder.response = "ignored"
            self._current_reminder.responded_at = datetime.now().isoformat()
            self.reminders_store.update(self._current_reminder)
            self._current_reminder = None

        self._clear_followup()
        now_iso = datetime.now().isoformat()

        # 创建 Reminder
        reminder = Reminder(
            id=uuid.uuid4().hex,
            triggered_at=now_iso,
            source=source,
        )
        self.reminders_store.append(reminder)
        self._current_reminder = reminder

        # DailyState
        self.cfg.today.last_reminder_ts = now_iso
        self.cfg.today.reminder_pending = True
        self.cfg.today.reminder_count += 1
        if self.cfg.today.session_started_at is None:
            self.cfg.today.session_started_at = now_iso
        self.cfg.save()

        # 响应窗口：超时视为不理（墙上时钟，_tick 里检查）
        self._pending_deadline = datetime.now() + timedelta(minutes=RESPONSE_WINDOW_MIN)

        # 刷新 UI 进入响应态
        self.refresh()

        # 下一个主周期重新计时（不管用户响应快慢）
        self.restart_reminder_cycle()

        # 判断是否需要唤起主窗口 + 托盘闪
        window_visible = self.isVisible() and not self.isMinimized() and self.isActiveWindow()
        if not window_visible:
            self._start_flash_and_raise()

        # 若挂起了跨日归档，处理
        if self._pending_rollover:
            self._pending_rollover = False
            self.check_day_rollover()

    def _on_response_timeout(self) -> None:
        """提醒弹出 RESPONSE_WINDOW_MIN 分钟无响应 = 不理。
        首次提醒（scheduled / catch_up）被不理 → 立即再提醒一次（第二次机会）。
        followup 提醒（推迟/不理产生）再被不理 → 按跳过惩罚，本轮结束。"""
        self._pending_deadline = None
        if not self.cfg.today.reminder_pending:
            return
        r = self._current_reminder
        second_chance = r is not None and r.source in ("snoozed_followup", "ignored_followup")
        if r is not None and r.response is None:
            r.response = "ignored"
            r.responded_at = datetime.now().isoformat()
            self.reminders_store.update(r)
        self._current_reminder = None
        self.cfg.today.reminder_pending = False
        self.cfg.save()
        if second_chance:
            self._apply_skip_penalty()
            self.cfg.save()
            self.refresh()
        else:
            self.refresh()
            self.show_reminder(source="ignored_followup")

    def _record_drink(self, ml: int, source: str = "response") -> None:
        """记一次喝水。

        source 语义：
        - response / supplement：需要 reminder_pending=True 才生效（单次幂等）
        - proactive：不消耗 pending，但受 PROACTIVE_COOLDOWN_MIN 冷却约束
        """
        now = datetime.now()
        now_iso = now.isoformat()

        if source == "proactive":
            # 物理合理性约束：距上次喝水记录不足冷却时长 → 拒记并给反馈。
            # 人不会 20 分钟内主动喝两次，连点只是给树浇假水
            remain = self._proactive_cooldown_remaining_min()
            if remain > 0:
                self._show_cooldown_feedback(remain)
                return
            # 主动喝一口：不消耗 pending、不清 current_reminder
            entry = DrinkEntry(
                id=uuid.uuid4().hex,
                timestamp=now_iso,
                amount_ml=ml,
                source="proactive",
                day_date=self.cfg.today.date,
                linked_reminder_id=None,
            )
            self.drink_entries_store.append(entry)
            self._last_drink_ts = now
            self.add_water(ml)
            # v3：任何"已喝"都清 skip_count（回到 s0 健康态）
            self.cfg.today.skip_count = 0
            self.cfg.save()
            # 达标后停周期不再提醒；未达标才重置周期
            goal = self.cfg.daily_goal_ml
            if goal > 0 and self.cfg.today.drunk_ml >= goal:
                self.stop_reminder_cycle()
            else:
                self.restart_reminder_cycle()
            self.refresh()
            return

        # response / supplement 走 pending 检查
        if not self.cfg.today.reminder_pending:
            return

        reminder_id = self._current_reminder.id if self._current_reminder else None
        entry = DrinkEntry(
            id=uuid.uuid4().hex,
            timestamp=now_iso,
            amount_ml=ml,
            source=source,
            day_date=self.cfg.today.date,
            linked_reminder_id=reminder_id,
        )
        self.drink_entries_store.append(entry)

        if self._current_reminder is not None:
            self._current_reminder.response = "drank"
            self._current_reminder.responded_at = now_iso
            self._current_reminder.actual_drunk_ml = ml
            self._current_reminder.linked_drink_entry_id = entry.id
            self.reminders_store.update(self._current_reminder)
            self._current_reminder = None

        self.add_water(ml)
        self._last_drink_ts = now
        # v3：已喝清 skip_count（回到 s0 健康态）
        self.cfg.today.skip_count = 0
        self.cfg.today.reminder_pending = False
        self.cfg.today.drank_count += 1
        self._pending_deadline = None
        self.cfg.save()
        # 达标后停周期不再提醒；未达标才重置周期
        goal = self.cfg.daily_goal_ml
        if goal > 0 and self.cfg.today.drunk_ml >= goal:
            self.stop_reminder_cycle()
        else:
            self.restart_reminder_cycle()
        self.refresh()
        # 响应完主窗口最小化到托盘（用户明确选 b：不占屏幕）
        self.hide()

    def _record_skip(self) -> None:
        if not self.cfg.today.reminder_pending:
            return
        now_iso = datetime.now().isoformat()
        if self._current_reminder is not None:
            self._current_reminder.response = "skipped"
            self._current_reminder.responded_at = now_iso
            self.reminders_store.update(self._current_reminder)
            self._current_reminder = None
        # v3：skip_count +1，到阈值触发视觉重置（视觉回种子，drunk_ml 保留）
        self._apply_skip_penalty()
        self.cfg.today.reminder_pending = False
        self._pending_deadline = None
        self.cfg.save()
        # 跳过 = 用户表态，重置主 timer，60 分钟后再问
        self.restart_reminder_cycle()
        self.refresh()
        self.hide()

    def _record_snooze(self) -> None:
        if self._current_reminder is not None:
            self._current_reminder.response = "snoozed"
            self._current_reminder.responded_at = datetime.now().isoformat()
            self.reminders_store.update(self._current_reminder)
            self._current_reminder = None
        self._pending_deadline = None
        self.cfg.today.reminder_pending = False
        # 5 分钟后再提醒：墙上时钟到期时间落盘，重启/睡眠唤醒后仍兑现
        self._schedule_followup("snoozed_followup")
        self.refresh()
        self.hide()

    # --- 响应态按钮 handler ---

    def _on_reminder_gulp(self) -> None:
        """喝一口：固定 50ml"""
        self._record_drink(50, source="response")

    def _on_reminder_half_cup(self) -> None:
        """喝半杯：per_cup_ml 的一半"""
        self._record_drink(self.cfg.per_cup_ml // 2, source="response")

    def _on_reminder_full_cup(self) -> None:
        """喝一杯：per_cup_ml"""
        self._record_drink(self.cfg.per_cup_ml, source="response")

    def _on_reminder_snooze(self) -> None:
        self._record_snooze()

    def _on_reminder_skip(self) -> None:
        self._record_skip()

    def _on_proactive_gulp(self) -> None:
        """常态 · 喝一口：固定 50ml"""
        self._record_drink(50, source="proactive")

    def _on_proactive_half_cup(self) -> None:
        """常态 · 喝半杯：per_cup_ml 的一半"""
        self._record_drink(self.cfg.per_cup_ml // 2, source="proactive")

    def _on_proactive_full_cup(self) -> None:
        """常态 · 喝一杯：per_cup_ml"""
        self._record_drink(self.cfg.per_cup_ml, source="proactive")

    # --- 托盘 flash + raise ---

    def _start_flash_and_raise(self) -> None:
        """show_reminder 触发时，主窗口不在前台则闪托盘 + 30 秒 raise 兜底。"""
        if self._tray_icon_ref is None:
            # 无托盘环境：直接 raise
            self._do_raise()
            return
        # 已有 flash 在跑则先停
        self._flash_timer.stop()
        self._flash_count = 0
        self._tray_icon_ref.setToolTip("该喝水啦")
        self._flash_timer.start()
        # 30 秒后 raise 主窗口
        if self._raise_timer is not None:
            self._raise_timer.stop()
        self._raise_timer = QTimer(self)
        self._raise_timer.setSingleShot(True)
        self._raise_timer.timeout.connect(self._on_flash_deadline)
        self._raise_timer.start(30 * 1000)

    def _on_flash_tick(self) -> None:
        if self._tray_icon_ref is None:
            self._flash_timer.stop()
            return
        self._flash_count += 1
        # 偶数用 A、奇数用 B
        if self._flash_count % 2 == 1:
            self._tray_icon_ref.setIcon(make_app_icon_highlight())
        else:
            self._tray_icon_ref.setIcon(make_app_icon())
        # 60 次到点
        if self._flash_count >= 60:
            self._flash_timer.stop()

    def _on_flash_deadline(self) -> None:
        """30 秒 raise 兜底。到点停 flash 并前置窗口。"""
        self._stop_flash()
        self._do_raise()

    def _do_raise(self) -> None:
        """前置主窗口。抢焦点一次，不做二次尝试。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _stop_flash(self) -> None:
        self._flash_timer.stop()
        self._flash_count = 0
        if self._tray_icon_ref is not None:
            self._tray_icon_ref.setIcon(make_app_icon())
            self._tray_icon_ref.setToolTip("喝水小助手")
        if self._raise_timer is not None:
            self._raise_timer.stop()
            self._raise_timer = None

    def showEvent(self, event) -> None:  # noqa: N802
        # 用户主动打开主窗口 → 立即停 flash
        if self._flash_timer.isActive() or self._flash_count > 0:
            self._stop_flash()
        # 位置纠错：pythonw / PyInstaller 打包 exe 启动时 Windows/Qt 有时把
        # 主窗口位置回填成 (-25600, -25600) 之类的屏幕外坐标，__init__ 里的
        # self.move() 会被 show() 之后的这次回填覆盖。这里每次 show 都验证：
        # 如果当前几何在可视屏幕外，强制拉回屏幕中心。
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            g = self.geometry()
            offscreen = (
                g.x() < avail.x() - 100
                or g.y() < avail.y() - 100
                or g.x() > avail.right() - 100
                or g.y() > avail.bottom() - 100
            )
            if offscreen:
                w = max(self.width(), self.minimumWidth())
                h = max(self.height(), self.minimumHeight())
                x = avail.x() + (avail.width() - w) // 2
                y = avail.y() + max(60, (avail.height() - h) // 3)
                self.move(x, y)
        super().showEvent(event)

    # --- 首次启动 Toast ---

    def maybe_show_first_launch_toast(self) -> None:
        """首次启动 1 秒后弹 Toast。位置在画布下方 overlay。"""
        if not self._first_launch:
            return
        QTimer.singleShot(1000, self._show_first_launch_toast)

    def _show_first_launch_toast(self) -> None:
        next_time = datetime.now() + timedelta(minutes=self.cfg.interval_min)
        hh_mm = next_time.strftime("%H:%M")
        text = f"我会每 {self.cfg.interval_min} 分钟提醒你喝水。下次提醒：{hh_mm}"
        toast = FirstLaunchToast(text, parent=self.centralWidget())
        self._toast = toast
        toast.show_at_canvas_bottom(self.canvas, self.action_zone)

    # --- 跨日归档 ---

    def check_day_rollover(self) -> None:
        """AC-3.4：dialog 显示中不归档，延后。"""
        today_str = date.today().isoformat()
        if self.cfg.today.date == today_str:
            return  # 未跨日
        if self._dialog_visible:
            self._pending_rollover = True
            return
        self._archive_and_reset(today_str)

    def _archive_and_reset(self, new_date: str) -> None:
        """AC-3.2 / 3.3：先写 HistoryEntry 成功后才清 DailyState。"""
        old_date = self.cfg.today.date
        if not old_date:
            # 首日空状态，无需归档，直接切换（AC-3.6）
            self.cfg.today = TodayState(
                date=new_date,
                daily_goal_ml_snapshot=self.cfg.daily_goal_ml,
            )
            self.cfg.save()
            self.refresh()
            return

        # 跨日时若还有 pending 中未响应的 Reminder，标 ignored 后落盘。
        # 语义跟"关 X 2 分钟超时"一致：跨日了用户就算永远不回来，
        # 之前那次提醒就是被忽略的，归档进 response_breakdown。
        if self._current_reminder is not None and self._current_reminder.response is None:
            self._current_reminder.response = "ignored"
            self._current_reminder.responded_at = datetime.now().isoformat()
            self.reminders_store.update(self._current_reminder)
            self._current_reminder = None
        self._pending_deadline = None

        # 聚合当天数据
        today_reminders = [
            r for r in self.reminders_store.load()
            if r.triggered_at[:10] == old_date
        ]
        today_entries = [
            e for e in self.drink_entries_store.load()
            if e.day_date == old_date
        ]
        breakdown = {"drank": 0, "skipped": 0, "snoozed": 0, "ignored": 0}
        for r in today_reminders:
            if r.response in breakdown:
                breakdown[r.response] += 1

        goal_snapshot = (
            self.cfg.today.daily_goal_ml_snapshot
            if self.cfg.today.daily_goal_ml_snapshot > 0
            else self.cfg.daily_goal_ml
        )
        progress = 0.0 if goal_snapshot <= 0 else min(1.0, self.cfg.today.drunk_ml / goal_snapshot)
        entry = HistoryEntry(
            date=old_date,
            drunk_ml_total=self.cfg.today.drunk_ml,
            daily_goal_ml_snapshot=goal_snapshot,
            is_goal_reached=self.cfg.today.drunk_ml >= goal_snapshot,
            final_growth_stage=_tree_stage(progress),
            reminder_count=self.cfg.today.reminder_count,
            response_breakdown=breakdown,
            drink_entry_ids=[e.id for e in today_entries],
            session_started_at=self.cfg.today.session_started_at,
            archived_at=datetime.now().isoformat(),
        )

        # TransactionGuard：先写 HistoryEntry，成功后才重置 DailyState
        try:
            self.history_store.append(entry)
        except Exception:
            # 写盘失败：保留 DailyState 旧值，下一分钟 timer 再试
            return

        self.cfg.today = TodayState(
            date=new_date,
            daily_goal_ml_snapshot=self.cfg.daily_goal_ml,
        )
        self.cfg.save()
        # 新的一天：如果昨天达标停了 timer，今天要重新启动
        self.restart_reminder_cycle()
        self.refresh()

    # --- 用户操作 ---

    def add_water(self, ml: int) -> None:
        self.cfg.today.drunk_ml += ml
        self.cfg.save()

    def on_manual_drink(self) -> None:
        """托盘菜单「我喝水了」入口。
        pending=True 时走 supplement 消耗 pending；pending=False 时走 proactive 主动记账。
        避免用户从托盘点了却因为 pending=False 静默失败。"""
        if self.cfg.today.reminder_pending:
            self._record_drink(self.cfg.per_cup_ml, source="supplement")
        else:
            self._record_drink(self.cfg.per_cup_ml, source="proactive")

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            dlg.apply_to(self.cfg)
            self.restart_reminder_cycle()
            self.refresh()

    # --- 托盘集成：关窗不退出 ---

    def closeEvent(self, event) -> None:  # noqa: N802
        if QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
        else:
            event.accept()


# ----------------------------- 托盘图标 -----------------------------


def make_app_icon() -> QIcon:
    """水滴图标。用 accent_leaf 苔绿，跟主视觉挂钩。"""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(THEME["accent_leaf"]))
    path = QPainterPath()
    path.moveTo(32, 6)
    path.cubicTo(60, 30, 56, 58, 32, 58)
    path.cubicTo(8, 58, 4, 30, 32, 6)
    p.drawPath(path)
    p.setBrush(QColor(255, 255, 255, 120))
    p.drawEllipse(QPointF(24, 26), 5, 8)
    p.end()
    return QIcon(pix)


def make_app_icon_highlight() -> QIcon:
    """flash 用的高亮版：整体变奶油/亮，跟正常版形成对比。"""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(THEME["accent_leaf"]), 3))
    p.setBrush(QColor(THEME["bg_elevated"]))
    path = QPainterPath()
    path.moveTo(32, 6)
    path.cubicTo(60, 30, 56, 58, 32, 58)
    path.cubicTo(8, 58, 4, 30, 32, 6)
    p.drawPath(path)
    # 高光更大更亮
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 200))
    p.drawEllipse(QPointF(24, 26), 7, 10)
    p.end()
    return QIcon(pix)


def make_gear_icon() -> QIcon:
    """齿轮图标。手画简单齿轮，避免图标字体依赖。"""
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    cx, cy = 16, 16
    outer_r = 12
    inner_r = 6
    tooth_len = 3
    # 齿：8 个方向的小突起
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(THEME["accent_bark"]))
    import math
    n_teeth = 8
    for i in range(n_teeth):
        angle = i * (2 * math.pi / n_teeth)
        x = cx + (outer_r + tooth_len - 1) * math.cos(angle)
        y = cy + (outer_r + tooth_len - 1) * math.sin(angle)
        p.drawEllipse(QPointF(x, y), 2.5, 2.5)
    # 主体环
    p.setBrush(QColor(THEME["accent_bark"]))
    p.drawEllipse(QPointF(cx, cy), outer_r, outer_r)
    # 中心镂空（用底色填一层）
    p.setBrush(QColor(THEME["bg_canvas"]))
    p.drawEllipse(QPointF(cx, cy), inner_r, inner_r)
    p.end()
    return QIcon(pix)


class FirstLaunchToast(QLabel):
    """首次启动 Toast。挂在 central widget 上做 overlay，入场 spring 出场 soft。
    位置：画布下方，行动区上方。3 秒自动消失，可点击立即消失。"""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(text, parent)
        self.setObjectName("first_launch_toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMaximumWidth(360)
        self.adjustSize()

        # 挂 opacity effect
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self._start_out)

        self._in_anim: Optional[QParallelAnimationGroup] = None
        self._out_anim: Optional[QParallelAnimationGroup] = None

    def show_at_canvas_bottom(self, canvas: QWidget, action_zone: QWidget) -> None:
        """居中显示在画布底部与行动区之间的空隙。"""
        parent = self.parentWidget()
        if parent is None:
            return
        # 目标 y：画布底端 + 4px（sp_1），保证不遮画布主体、也不撞行动区
        self.adjustSize()
        canvas_rect = canvas.geometry()
        target_x = (parent.width() - self.width()) // 2
        target_y = canvas_rect.bottom() + 4
        # 保证不超出 action_zone 顶部
        max_y = action_zone.geometry().top() - self.height() - 4
        if target_y > max_y and max_y > canvas_rect.top():
            target_y = max_y
        # 入场从 +12px 位移过来
        start_y = target_y + 12
        self.move(target_x, start_y)
        self.raise_()
        self.show()

        if not THEME["motion_enabled"]:
            self.move(target_x, target_y)
            self._effect.setOpacity(1.0)
            self._dismiss_timer.start(3000)
            return

        # 入场：opacity + pos 并行
        opacity_anim = QPropertyAnimation(self._effect, b"opacity", self)
        opacity_anim.setDuration(THEME["dur_slow"])
        opacity_anim.setStartValue(0.0)
        opacity_anim.setEndValue(1.0)
        opacity_anim.setEasingCurve(QEasingCurve.OutQuart)

        pos_anim = QPropertyAnimation(self, b"pos", self)
        pos_anim.setDuration(THEME["dur_slow"])
        pos_anim.setStartValue(QPoint(target_x, start_y))
        pos_anim.setEndValue(QPoint(target_x, target_y))
        pos_anim.setEasingCurve(QEasingCurve.OutBack)

        group = QParallelAnimationGroup(self)
        group.addAnimation(opacity_anim)
        group.addAnimation(pos_anim)
        group.finished.connect(lambda: self._dismiss_timer.start(3000))
        group.start()
        self._in_anim = group

    def _start_out(self) -> None:
        if not THEME["motion_enabled"]:
            self.deleteLater()
            return
        target_x = self.x()
        target_y = self.y()

        opacity_anim = QPropertyAnimation(self._effect, b"opacity", self)
        opacity_anim.setDuration(THEME["dur_normal"])
        opacity_anim.setStartValue(1.0)
        opacity_anim.setEndValue(0.0)
        opacity_anim.setEasingCurve(QEasingCurve.OutQuart)

        pos_anim = QPropertyAnimation(self, b"pos", self)
        pos_anim.setDuration(THEME["dur_normal"])
        pos_anim.setStartValue(QPoint(target_x, target_y))
        pos_anim.setEndValue(QPoint(target_x, target_y - 6))
        pos_anim.setEasingCurve(QEasingCurve.OutQuart)

        group = QParallelAnimationGroup(self)
        group.addAnimation(opacity_anim)
        group.addAnimation(pos_anim)
        group.finished.connect(self.deleteLater)
        group.start()
        self._out_anim = group

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 点击立即消失
        self._dismiss_timer.stop()
        self._start_out()
        super().mousePressEvent(event)


def setup_tray(app: QApplication, window: MainWindow) -> Optional[QSystemTrayIcon]:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    tray = QSystemTrayIcon(make_app_icon(), parent=app)
    tray.setToolTip("喝水小助手")

    menu = QMenu()
    act_show = QAction("显示主窗口", menu)
    act_show.triggered.connect(lambda: (window.showNormal(), window.activateWindow()))
    menu.addAction(act_show)

    act_drink = QAction("我喝水了", menu)
    act_drink.triggered.connect(window.on_manual_drink)
    menu.addAction(act_drink)

    menu.addSeparator()
    act_quit = QAction("退出", menu)
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_quit)

    tray.setContextMenu(menu)

    def on_activated(reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            window.showNormal()
            window.activateWindow()

    tray.activated.connect(on_activated)
    tray.show()
    window.attach_tray(tray)
    return tray


# ----------------------------- 入口 -----------------------------


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关窗不退出，靠托盘
    app.setApplicationName("喝水小助手")

    # 探测 config.json 是否存在，用来判断首次启动（Config.load 会创建它，所以要提前查）
    is_first_launch = not CONFIG_PATH.exists()
    cfg = Config.load()
    window = MainWindow(cfg)
    window._first_launch = is_first_launch
    window.show()

    tray = setup_tray(app, window)
    # 主窗口渲染后触发首次启动 Toast（延后 1 秒，避开入场动画重叠）
    window.maybe_show_first_launch_toast()
    if tray is None:
        # 没有系统托盘的极端情况：允许关窗即退出
        app.setQuitOnLastWindowClosed(True)
        QMessageBox.information(
            window,
            "提示",
            "当前系统未启用托盘，关闭主窗口程序会退出。",
        )

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
