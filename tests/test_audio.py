"""程序化音频测试：WAV 合成、缓存、设置持久化与声音控件同步。

测试在 Qt offscreen 平台运行；AudioManager 自动禁用真实音频设备，
不会访问用户注册表中的设置。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
import wave
from array import array
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QSettings, QThread, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

import gomoku.client.audio as audio_module  # noqa: E402
from gomoku.client.audio import (  # noqa: E402
    ASSET_BGM,
    ASSET_BUTTON,
    ASSET_MOVE,
    DEFAULT_BGM_VOLUME,
    DEFAULT_SFX_VOLUME,
    SAMPLE_RATE,
    AudioManager,
    assets_directory,
    decode_audio_to_wav_bytes,
    ensure_cached_wav,
    ensure_decoded_wav,
    find_audio_asset,
    find_cached_wav,
    synthesize_sound,
)
from gomoku.client.sound_control import SoundButton  # noqa: E402

try:  # pragma: no cover - 裁剪环境可能没有 QtMultimedia
    from PySide6.QtMultimedia import QSoundEffect
except ImportError:  # pragma: no cover
    QSoundEffect = None  # type: ignore[assignment]

QT_APP = QApplication.instance() or QApplication([])

_SFX_KINDS = ("move", "start", "win", "lose", "draw", "tick")
_EXPECTED_DURATION = {
    "move": 0.14,
    "start": 1.1,
    "win": 1.8,
    "lose": 1.9,
    "draw": 1.1,
    "tick": 0.24,
}


def _read_wav(data: bytes) -> tuple[int, int, int, int, bytes]:
    with wave.open(io.BytesIO(data), "rb") as wav_file:
        return (
            wav_file.getnchannels(),
            wav_file.getsampwidth(),
            wav_file.getframerate(),
            wav_file.getnframes(),
            wav_file.readframes(wav_file.getnframes()),
        )


def _peak(frames: bytes) -> int:
    samples = array("h")
    samples.frombytes(frames)
    return max((abs(sample) for sample in samples), default=0)


class SynthesisTests(unittest.TestCase):
    """合成结果必须是可播放、非静音且确定性的 WAV。"""

    def test_sfx_format_duration_and_signal(self) -> None:
        for kind in _SFX_KINDS:
            with self.subTest(kind=kind):
                channels, width, rate, frames, pcm = _read_wav(
                    synthesize_sound(kind)
                )
                self.assertEqual(channels, 1)
                self.assertEqual(width, 2)
                self.assertEqual(rate, SAMPLE_RATE)
                self.assertGreater(frames, 0)
                self.assertAlmostEqual(
                    frames / SAMPLE_RATE, _EXPECTED_DURATION[kind], delta=0.02
                )
                self.assertGreater(_peak(pcm), 500)

    def test_bgm_short_preview(self) -> None:
        data = synthesize_sound("bgm", duration_seconds=1.0)
        channels, width, rate, frames, pcm = _read_wav(data)
        self.assertEqual(channels, 1)
        self.assertEqual(width, 2)
        self.assertEqual(rate, SAMPLE_RATE)
        self.assertEqual(frames, round(1.0 * SAMPLE_RATE))
        self.assertGreater(_peak(pcm), 500)

    def test_synthesis_is_deterministic(self) -> None:
        for kind in _SFX_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(
                    synthesize_sound(kind), synthesize_sound(kind)
                )
        self.assertEqual(
            synthesize_sound("bgm", duration_seconds=0.8),
            synthesize_sound("bgm", duration_seconds=0.8),
        )

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            synthesize_sound("unknown")  # type: ignore[arg-type]


class CacheTests(unittest.TestCase):
    """缓存应复用合法文件、修复损坏文件。"""

    def test_cache_reuse_and_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            path = ensure_cached_wav("move", cache_dir)
            self.assertTrue(path.exists())
            self.assertEqual(find_cached_wav("move", cache_dir), path)
            expected = path.read_bytes()

            modified = path.stat().st_mtime_ns
            time.sleep(0.01)
            self.assertEqual(ensure_cached_wav("move", cache_dir), path)
            self.assertEqual(path.stat().st_mtime_ns, modified)

            path.write_bytes(b"not-a-wav")
            self.assertIsNone(find_cached_wav("move", cache_dir))
            repaired = ensure_cached_wav("move", cache_dir)
            self.assertEqual(repaired, path)
            self.assertEqual(path.read_bytes(), expected)


class AssetPipelineTests(unittest.TestCase):
    """内置 MP3 素材可发现、可解码为可播放 WAV 并缓存。"""

    def test_bundled_assets_exist(self) -> None:
        for name in (ASSET_BGM, ASSET_MOVE, ASSET_BUTTON):
            with self.subTest(name=name):
                self.assertTrue((assets_directory() / name).is_file())
                self.assertIsNotNone(find_audio_asset(name))

    def test_sfx_mp3_decode_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            for kind, name in (("move", ASSET_MOVE), ("button", ASSET_BUTTON)):
                with self.subTest(kind=kind):
                    source = find_audio_asset(name)
                    self.assertIsNotNone(source)

                    data = decode_audio_to_wav_bytes(source)  # type: ignore[arg-type]
                    self.assertIsNotNone(data)
                    channels, width, rate, frames, pcm = _read_wav(data or b"")
                    self.assertEqual(channels, 1)
                    self.assertEqual(width, 2)
                    self.assertGreater(rate, 0)
                    self.assertGreater(frames, 0)
                    self.assertGreater(_peak(pcm), 500)

                    path = ensure_decoded_wav(kind, source, cache_dir)  # type: ignore[arg-type]
                    self.assertIsNotNone(path)
                    assert path is not None
                    self.assertTrue(path.is_file())
                    modified = path.stat().st_mtime_ns
                    self.assertEqual(
                        ensure_decoded_wav(kind, source, cache_dir), path  # type: ignore[arg-type]
                    )
                    self.assertEqual(path.stat().st_mtime_ns, modified)


class _FakeEffect:
    """只实现音效池用到的接口的假音效实例。"""

    def __init__(self, status: object) -> None:
        self._status = status
        self.play_count = 0
        self.stop_count = 0
        self.volume = 0.0

    def status(self) -> object:
        return self._status

    def play(self) -> None:
        self.play_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def setVolume(self, volume: float) -> None:  # noqa: N802 - Qt 接口
        self.volume = float(volume)


class DecodeThreadTests(unittest.TestCase):
    """解码必须离开主线程：QAudioDecoder 不能跨线程使用。"""

    def test_decode_runs_on_a_worker_thread(self) -> None:
        source = find_audio_asset(ASSET_MOVE)
        self.assertIsNotNone(source)
        assert source is not None

        threads: list[QThread] = []
        original = audio_module._decode_audio_bytes

        def spy(path, cancel_check=None):
            threads.append(QThread.currentThread())
            return original(path, cancel_check)

        with mock.patch.object(
            audio_module, "_decode_audio_bytes", spy
        ):
            data = decode_audio_to_wav_bytes(source)

        self.assertIsNotNone(data)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], QT_APP.thread())

    def test_startup_does_not_block_on_decode(self) -> None:
        """启动流程里的解码在后台线程进行，不能把主线程卡住。"""
        source = find_audio_asset(ASSET_MOVE)
        self.assertIsNotNone(source)
        assert source is not None

        threads: list[QThread] = []
        original = audio_module._decode_audio_bytes

        def slow_spy(path, cancel_check=None):
            threads.append(QThread.currentThread())
            time.sleep(1.0)
            return original(path, cancel_check)

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            settings = QSettings(
                str(cache_dir / "audio.ini"), QSettings.Format.IniFormat
            )
            with mock.patch.object(
                audio_module, "_decode_audio_bytes", slow_spy
            ), mock.patch.object(
                AudioManager, "_prepare_bgm", lambda self: None
            ), mock.patch.object(
                # 不创建真实音效实例：它们会一直占着缓存文件。
                AudioManager, "_install_effects", lambda self, paths: None
            ):
                started = time.monotonic()
                manager = AudioManager(
                    enabled=True, settings=settings, cache_dir=cache_dir
                )
                elapsed = time.monotonic() - started
                try:
                    # 构造过程没有等解码完成。
                    self.assertLess(elapsed, 0.9)
                    # 等解码完成：缓存文件出现。
                    deadline = time.monotonic() + 30.0
                    cached: list[Path] = []
                    while time.monotonic() < deadline:
                        QT_APP.processEvents()
                        cached = list(cache_dir.glob("move_src*.wav"))
                        if cached:
                            break
                        time.sleep(0.01)
                    # 缓存仍然生成。
                    self.assertTrue(cached, "解码缓存没有生成")
                    # 解码跑在后台线程里。
                    self.assertTrue(threads)
                    self.assertNotEqual(threads[0], QT_APP.thread())
                finally:
                    manager.shutdown()


class EffectPoolTests(unittest.TestCase):
    """音效池的补播与忙碌判断。"""

    def _manager(self) -> AudioManager:
        tmpdir = tempfile.TemporaryDirectory()
        manager = AudioManager(
            enabled=False,
            settings=QSettings(
                str(Path(tmpdir.name) / "audio.ini"),
                QSettings.Format.IniFormat,
            ),
        )
        # 离屏环境没有真实音频设备，这里直接打开开关并塞入假实例。
        manager._available = True
        # 临时目录要活到用例结束。
        manager._test_tmpdir = tmpdir
        return manager

    @unittest.skipIf(QSoundEffect is None, "缺少 QtMultimedia")
    def test_move_effect_is_catched_up_while_loading(self) -> None:
        """加载中的落子音效也要补播，不能被静默丢弃。"""
        manager = self._manager()
        try:
            effect = _FakeEffect(QSoundEffect.Status.Loading)
            manager._effects["move"] = [effect]
            manager.play_move()
            self.assertIn("move", manager._pending_oneshots)
        finally:
            manager.shutdown()

    @unittest.skipIf(QSoundEffect is None, "缺少 QtMultimedia")
    def test_pool_uses_every_live_instance(self) -> None:
        """轮到的实例没就绪时，要看池子里其它实例。"""
        manager = self._manager()
        try:
            loading = _FakeEffect(QSoundEffect.Status.Loading)
            ready = _FakeEffect(QSoundEffect.Status.Ready)
            manager._effects["move"] = [loading, ready]
            manager._sfx_pool_index = 0
            manager.play_move()
            self.assertEqual(ready.play_count, 1)
            self.assertEqual(loading.play_count, 0)
        finally:
            manager.shutdown()

    @unittest.skipIf(QSoundEffect is None, "缺少 QtMultimedia")
    def test_error_is_reported_only_when_all_instances_fail(self) -> None:
        """只有一个实例失败时，整种音效不算不可用。"""
        manager = self._manager()
        try:
            ready = _FakeEffect(QSoundEffect.Status.Ready)
            failed = _FakeEffect(QSoundEffect.Status.Error)
            manager._effects["move"] = [ready, failed]
            manager._on_effect_status_changed("move", failed)
            self.assertNotIn("move", manager._error_logged)
        finally:
            manager.shutdown()

    @unittest.skipIf(QSoundEffect is None, "缺少 QtMultimedia")
    def test_pending_effect_plays_when_a_later_instance_is_ready(self) -> None:
        """第二个实例先就绪时，补播要由这个实例发出。"""
        manager = self._manager()
        try:
            loading = _FakeEffect(QSoundEffect.Status.Loading)
            ready = _FakeEffect(QSoundEffect.Status.Ready)
            manager._effects["move"] = [loading, ready]
            manager._pending_oneshots.add("move")
            manager._on_effect_status_changed("move", ready)
            self.assertEqual(ready.play_count, 1)
            self.assertNotIn("move", manager._pending_oneshots)
        finally:
            manager.shutdown()


class ButtonClickFilterTests(unittest.TestCase):

    def test_button_press_plays_click_sound(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.calls = 0

            def play_button(self) -> None:
                self.calls += 1

        fake = FakeManager()
        button = QPushButton("测试")
        button.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen, True
        )
        click_filter = audio_module._ButtonClickFilter(  # type: ignore[arg-type]
            fake
        )
        button.installEventFilter(click_filter)
        button.show()
        QT_APP.processEvents()
        try:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.assertEqual(fake.calls, 1)
        finally:
            button.close()
            button.deleteLater()
            QT_APP.processEvents()


class AudioManagerTests(unittest.TestCase):
    """禁用环境下的设置钳制、持久化与安全空操作。"""

    @staticmethod
    def _settings(directory: Path) -> QSettings:
        return QSettings(
            str(directory / "audio.ini"), QSettings.Format.IniFormat
        )

    def test_defaults_clamping_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            manager = AudioManager(
                enabled=False, settings=self._settings(cache_dir)
            )
            try:
                self.assertFalse(manager.available)
                self.assertFalse(manager.muted)
                self.assertEqual(manager.bgm_volume, DEFAULT_BGM_VOLUME)
                self.assertEqual(manager.sfx_volume, DEFAULT_SFX_VOLUME)

                changes: list[int] = []
                manager.state_changed.connect(lambda: changes.append(1))
                manager.set_muted(True)
                manager.set_bgm_volume(150)
                manager.set_sfx_volume(-5)
                self.assertEqual(
                    (manager.muted, manager.bgm_volume, manager.sfx_volume),
                    (True, 100, 0),
                )
                self.assertEqual(len(changes), 3)

                manager.play_start()
                manager.play_move()
                manager.play_win()
                manager.play_lose()
                manager.play_draw()
            finally:
                manager.shutdown()

            reloaded = AudioManager(
                enabled=False, settings=self._settings(cache_dir)
            )
            try:
                self.assertTrue(reloaded.muted)
                self.assertEqual(reloaded.bgm_volume, 100)
                self.assertEqual(reloaded.sfx_volume, 0)
            finally:
                reloaded.shutdown()

    def test_singleton_is_disabled_offscreen(self) -> None:
        self.assertIn(
            QGuiApplication.platformName().lower(), {"offscreen", "minimal"}
        )
        previous = audio_module._manager
        audio_module._manager = None
        try:
            manager = audio_module.get_audio_manager()
            self.assertFalse(manager.available)
            manager.play_move()  # 不应访问音频设备或抛出异常
        finally:
            audio_module._manager = previous


class SoundButtonTests(unittest.TestCase):
    """按钮控件与共享管理器保持双向同步。"""

    def test_button_syncs_both_ways(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = AudioManager(
                enabled=False,
                settings=QSettings(
                    str(Path(directory) / "button.ini"),
                    QSettings.Format.IniFormat,
                ),
            )
            button = SoundButton(manager=manager)
            try:
                self.assertFalse(button._mute_action.isChecked())

                manager.set_muted(True)
                self.assertTrue(button._mute_action.isChecked())

                manager.set_bgm_volume(10)
                manager.set_sfx_volume(20)
                self.assertEqual(button._bgm_slider.value(), 10)
                self.assertEqual(button._sfx_slider.value(), 20)
                self.assertEqual(button._bgm_value.text(), "10%")

                button._bgm_slider.setValue(30)
                button._sfx_slider.setValue(40)
                self.assertEqual(manager.bgm_volume, 30)
                self.assertEqual(manager.sfx_volume, 40)
            finally:
                button.deleteLater()
                manager.shutdown()


__all__ = [
    "AssetPipelineTests",
    "AudioManagerTests",
    "ButtonClickFilterTests",
    "CacheTests",
    "DecodeThreadTests",
    "EffectPoolTests",
    "SoundButtonTests",
    "SynthesisTests",
]
