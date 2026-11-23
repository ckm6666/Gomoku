"""客户端更新清单接口测试。"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.app.api import updates as updates_module  # noqa: E402
from server.app.config import Settings  # noqa: E402
from server.tests.helpers import ServerTestBase  # noqa: E402

MANIFEST_URL = "https://example.com/Gomoku-1.12.0.exe"

VALID_MANIFEST: dict[str, Any] = {
    "version": "1.12.0",
    "url": MANIFEST_URL,
    "sha256": "a" * 64,
    "size": 12345678,
    "notes": "更新说明",
    "published_at": "2026-01-01T00:00:00Z",
}


class UpdatesTestBase(ServerTestBase):
    """带更新清单路径开关的测试基类。"""

    def setUp(self) -> None:
        super().setUp()
        self.manifest_dir = Path(self._tmpdir.name)
        # 安装包目录单独放在子目录里，便于验证越界访问被拒绝。
        self.artifacts_dir = self.manifest_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def use_manifest_path(self, path: Optional[str]) -> None:
        """替换运行期配置里的更新清单路径。"""
        self.app.state.settings = replace(
            self.settings, update_manifest_path=path
        )

    def use_artifacts_dir(self, path: Optional[str]) -> None:
        """替换运行期配置里的安装包目录（同时清掉清单文件路径）。"""
        self.app.state.settings = replace(
            self.settings,
            update_manifest_path=None,
            update_artifacts_dir=path,
        )

    def write_manifest(self, payload: Any) -> Path:
        """把清单写到临时目录，返回文件路径。"""
        path = self.manifest_dir / "manifest.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        return path

    def write_artifact(self, filename: str, payload: bytes) -> Path:
        """写一个假的安装包，返回文件路径。"""
        path = self.artifacts_dir / filename
        path.write_bytes(payload)
        return path


class LatestUpdateTest(UpdatesTestBase):
    def test_unconfigured_manifest_returns_503(self) -> None:
        self.use_manifest_path(None)
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(503, response.status_code)
        self.assertEqual("服务端未配置更新清单", response.json()["detail"])

    def test_valid_manifest_returns_200(self) -> None:
        path = self.write_manifest(VALID_MANIFEST)
        self.use_manifest_path(str(path))
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("1.12.0", payload["version"])
        self.assertEqual(MANIFEST_URL, payload["url"])
        self.assertEqual("a" * 64, payload["sha256"])
        self.assertEqual(12345678, payload["size"])
        self.assertEqual("更新说明", payload["notes"])
        self.assertEqual("2026-01-01T00:00:00Z", payload["published_at"])

    def test_manifest_without_version_returns_503(self) -> None:
        payload = dict(VALID_MANIFEST)
        payload.pop("version")
        path = self.write_manifest(payload)
        self.use_manifest_path(str(path))
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(503, response.status_code)
        self.assertEqual("更新清单格式不正确", response.json()["detail"])

    def test_manifest_with_non_http_url_returns_503(self) -> None:
        payload = dict(VALID_MANIFEST)
        payload["url"] = "file:///C:/Gomoku.exe"
        path = self.write_manifest(payload)
        self.use_manifest_path(str(path))
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(503, response.status_code)
        self.assertEqual("更新清单格式不正确", response.json()["detail"])

    def test_missing_manifest_file_returns_404(self) -> None:
        missing = self.manifest_dir / "not-exists.json"
        self.use_manifest_path(str(missing))
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(404, response.status_code)
        self.assertEqual("更新清单文件不存在", response.json()["detail"])

    def test_malformed_manifest_returns_500(self) -> None:
        path = self.write_manifest("{ not json ")
        self.use_manifest_path(str(path))
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(500, response.status_code)
        self.assertEqual("更新清单文件无法读取", response.json()["detail"])

    def test_endpoint_is_public(self) -> None:
        """未登录也能访问：客户端启动时可能还没有 token。"""
        path = self.write_manifest(VALID_MANIFEST)
        self.use_manifest_path(str(path))
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("www-authenticate", response.headers)

    def test_manifest_path_wins_over_artifacts_dir(self) -> None:
        """两者都配置时以清单文件为准，保持旧行为不变。"""
        path = self.write_manifest(VALID_MANIFEST)
        self.write_artifact("Gomoku-9.9.9.exe", b"binary")
        self.app.state.settings = replace(
            self.settings,
            update_manifest_path=str(path),
            update_artifacts_dir=str(self.artifacts_dir),
        )
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(200, response.status_code)
        self.assertEqual(MANIFEST_URL, response.json()["url"])


class ArtifactsManifestTest(UpdatesTestBase):
    """服务端自行托管安装包时生成的清单。"""

    def test_artifacts_manifest_returns_200(self) -> None:
        content = b"fake-executable-content"
        path = self.write_artifact("Gomoku-1.12.0.exe", content)
        self.use_artifacts_dir(str(self.artifacts_dir))

        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("1.12.0", payload["version"])
        self.assertTrue(
            payload["url"].endswith(
                "/api/v1/updates/download/Gomoku-1.12.0.exe"
            ),
            payload["url"],
        )
        self.assertEqual(
            hashlib.sha256(content).hexdigest(), payload["sha256"]
        )
        self.assertEqual(path.stat().st_size, payload["size"])
        self.assertEqual("", payload["notes"])
        self.assertTrue(payload["published_at"])

    def test_highest_version_wins(self) -> None:
        self.write_artifact("Gomoku-1.9.0.exe", b"old")
        self.write_artifact("Gomoku-1.12.0.exe", b"new")
        self.write_artifact("Gomoku-1.10.3.exe", b"middle")
        self.use_artifacts_dir(str(self.artifacts_dir))

        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(200, response.status_code)
        self.assertEqual("1.12.0", response.json()["version"])

    def test_unrelated_files_are_ignored(self) -> None:
        self.write_artifact("Gomoku-latest.exe", b"nope")
        self.write_artifact("notes.txt", b"nope")
        self.use_artifacts_dir(str(self.artifacts_dir))

        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(404, response.status_code)
        self.assertEqual("尚未上传任何更新包", response.json()["detail"])

    def test_empty_artifacts_dir_returns_404(self) -> None:
        self.use_artifacts_dir(str(self.artifacts_dir))
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(404, response.status_code)
        self.assertEqual("尚未上传任何更新包", response.json()["detail"])

    def test_sidecar_provides_notes_and_published_at(self) -> None:
        self.write_artifact("Gomoku-1.12.0.exe", b"binary")
        (self.artifacts_dir / "update.json").write_text(
            json.dumps(
                {
                    "notes": "修复若干问题",
                    "published_at": "2026-02-03T04:05:06Z",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.use_artifacts_dir(str(self.artifacts_dir))

        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("修复若干问题", payload["notes"])
        self.assertEqual("2026-02-03T04:05:06Z", payload["published_at"])

    def test_unconfigured_returns_503(self) -> None:
        self.use_artifacts_dir(None)
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(503, response.status_code)
        self.assertEqual("服务端未配置更新清单", response.json()["detail"])

    def test_missing_artifacts_dir_returns_404(self) -> None:
        missing = self.artifacts_dir / "no-such-dir"
        self.use_artifacts_dir(str(missing))
        response = self.client.get("/api/v1/updates/latest")
        self.assertEqual(404, response.status_code)
        self.assertEqual("尚未上传任何更新包", response.json()["detail"])

    def test_sha256_is_cached_between_requests(self) -> None:
        """同一份安装包重复请求时不应重新计算摘要。"""
        self.write_artifact("Gomoku-1.12.0.exe", b"binary")
        self.use_artifacts_dir(str(self.artifacts_dir))

        first = self.client.get("/api/v1/updates/latest")
        self.assertEqual(200, first.status_code)
        digest = first.json()["sha256"]
        self.assertTrue(
            any(
                key[0].endswith("Gomoku-1.12.0.exe")
                for key in updates_module._SHA256_CACHE
            )
        )

        second = self.client.get("/api/v1/updates/latest")
        self.assertEqual(200, second.status_code)
        self.assertEqual(digest, second.json()["sha256"])


class DownloadUpdateTest(UpdatesTestBase):
    """安装包下载接口。"""

    def test_download_returns_file(self) -> None:
        content = b"fake-executable-content"
        self.write_artifact("Gomoku-1.12.0.exe", content)
        self.use_artifacts_dir(str(self.artifacts_dir))

        response = self.client.get(
            "/api/v1/updates/download/Gomoku-1.12.0.exe"
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(content, response.content)
        self.assertIn(
            "Gomoku-1.12.0.exe", response.headers["content-disposition"]
        )
        self.assertEqual(
            "application/vnd.microsoft.portable-executable",
            response.headers["content-type"],
        )

    def test_download_missing_file_returns_404(self) -> None:
        self.use_artifacts_dir(str(self.artifacts_dir))
        response = self.client.get(
            "/api/v1/updates/download/Gomoku-1.12.0.exe"
        )
        self.assertEqual(404, response.status_code)
        self.assertNotIn(str(self.artifacts_dir), response.text)

    def test_download_rejects_traversal(self) -> None:
        secret = self.manifest_dir / "secret.txt"
        secret.write_text("top secret", encoding="utf-8")
        self.write_artifact("Gomoku-1.12.0.exe", b"binary")
        self.use_artifacts_dir(str(self.artifacts_dir))

        response = self.client.get(
            "/api/v1/updates/download/..%2Fsecret.txt"
        )
        self.assertEqual(404, response.status_code)
        self.assertNotIn("top secret", response.text)
        self.assertNotIn(str(self.artifacts_dir), response.text)

    def test_download_rejects_nested_path(self) -> None:
        self.use_artifacts_dir(str(self.artifacts_dir))
        response = self.client.get(
            "/api/v1/updates/download/sub%2Fgomoku.exe"
        )
        self.assertEqual(404, response.status_code)

    def test_download_rejects_drive_letter(self) -> None:
        self.use_artifacts_dir(str(self.artifacts_dir))
        response = self.client.get(
            "/api/v1/updates/download/C%3Awindows.exe"
        )
        self.assertEqual(404, response.status_code)
        self.assertNotIn(str(self.artifacts_dir), response.text)

    def test_download_without_artifacts_dir_returns_404(self) -> None:
        self.use_artifacts_dir(None)
        response = self.client.get(
            "/api/v1/updates/download/Gomoku-1.12.0.exe"
        )
        self.assertEqual(404, response.status_code)
        self.assertEqual("服务端未配置更新包目录", response.json()["detail"])

    def test_filename_guard_rejects_unsafe_names(self) -> None:
        """下载接口的文件名白名单检查。"""
        for name in (
            "",
            ".",
            "..",
            "../secret.txt",
            "..\\secret.txt",
            "sub/dir.exe",
            "sub\\dir.exe",
            "C:windows.exe",
            "C:\\windows.exe",
            "bad\x00name.exe",
        ):
            self.assertFalse(
                updates_module._is_safe_filename(name), name
            )
        self.assertTrue(updates_module._is_safe_filename("Gomoku-1.12.0.exe"))


class UpdateSettingsTest(unittest.TestCase):
    """更新相关的环境变量读取。"""

    def test_artifacts_dir_read_from_env(self) -> None:
        settings = Settings.from_env(
            {
                "GOMOKU_JWT_SECRET": "s" * 48,
                "GOMOKU_UPDATE_ARTIFACTS_DIR": "  C:/artifacts  ",
            }
        )
        self.assertEqual("C:/artifacts", settings.update_artifacts_dir)

    def test_artifacts_dir_defaults_to_none(self) -> None:
        settings = Settings.from_env({"GOMOKU_JWT_SECRET": "s" * 48})
        self.assertIsNone(settings.update_artifacts_dir)
        self.assertIsNone(settings.update_manifest_path)


if __name__ == "__main__":
    unittest.main()
