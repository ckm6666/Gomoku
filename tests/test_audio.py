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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QSettings, Qt  # noqa: E402
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


class ButtonClickFilterTests(unittest.TestCase):
    """应用级事件过滤器把按钮按下映射为点击音效。"""

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
    "SoundButtonTests",
    "SynthesisTests",
]
