"""AI 复盘纯逻辑测试：评分映射、标注与逐手分析结构。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.client.ai_engine import (  # noqa: E402
    AIDifficulty,
    CandidateScore,
    SearchAnalysis,
)
from gomoku.client.review import (  # noqa: E402
    LABEL_BEST,
    LABEL_BLUNDER,
    LABEL_GOOD,
    LABEL_INACCURATE,
    LABEL_MISTAKE,
    accuracy_percent,
    classify_loss,
    review_record,
    score_to_win_rate,
)
from gomoku.shared.record import GameRecord  # noqa: E402
from gomoku.shared.rules import Player  # noqa: E402


def _analysis(
    best_move, best_score: int, player: Player
) -> SearchAnalysis:
    return SearchAnalysis(
        best_move=best_move,
        best_score=best_score,
        candidates=(
            CandidateScore(move=best_move, score=best_score),
        ) if best_move else (),
        player=player,
        difficulty=AIDifficulty.MASTER,
        completed_depth=3,
        nodes=100,
        elapsed_seconds=0.01,
        cancelled=False,
        timed_out=False,
        source="search",
    )


class ReviewScoringTests(unittest.TestCase):
    def test_classify_loss_thresholds(self) -> None:
        self.assertEqual(classify_loss(0, is_best=True), LABEL_BEST)
        self.assertEqual(classify_loss(10), LABEL_GOOD)
        self.assertEqual(classify_loss(900), LABEL_INACCURATE)
        self.assertEqual(classify_loss(2_000), LABEL_MISTAKE)
        self.assertEqual(classify_loss(20_000), LABEL_BLUNDER)

    def test_win_rate_is_monotonic_and_bounded(self) -> None:
        self.assertAlmostEqual(score_to_win_rate(0), 0.5, places=3)
        self.assertGreater(score_to_win_rate(3_000), score_to_win_rate(0))
        self.assertLess(score_to_win_rate(-3_000), score_to_win_rate(0))
        self.assertGreaterEqual(score_to_win_rate(10_000_000), 0.99)
        self.assertLessEqual(score_to_win_rate(-10_000_000), 0.01)

    def test_accuracy_percent(self) -> None:
        self.assertEqual(accuracy_percent(0), 100)
        self.assertEqual(accuracy_percent(6000), 0)
        self.assertGreater(accuracy_percent(600), 0)


class _ScriptedAI:
    """按调用顺序返回预设分析结果的假引擎。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def analyze(
        self,
        board_snapshot,
        player,
        difficulty,
        cancel_event=None,
        *,
        top_n=3,
        config_override=None,
    ):
        self.calls += 1
        if not self._responses:
            return None
        best_move, score = self._responses.pop(0)
        return _analysis(best_move, score, player)


def _record() -> GameRecord:
    return GameRecord.from_dict(
        {
            "version": 1,
            "record_id": "review-1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "mode": "local",
            "winner": None,
            "moves": [
                {"row": 7, "col": 7, "player": "black"},
                {"row": 7, "col": 8, "player": "white"},
                {"row": 7, "col": 9, "player": "black"},
            ],
        }
    )


class ReviewRecordTests(unittest.TestCase):
    def test_reviews_each_move_and_summarizes(self) -> None:
        ai = _ScriptedAI(
            [
                ((7, 7), 500),  # 第 1 手：实战即最佳
                ((0, 0), 400),  # 第 2 手：最佳在别处
                ((1, 1), 300),  # 第 2 手落子后对手分析
                ((0, 0), 300),  # 第 3 手：最佳在别处
                ((2, 2), 1_000),  # 第 3 手落子后对手分析
            ]
        )
        progress: list[tuple[int, int]] = []
        result = review_record(
            _record(),
            ai,
            progress=lambda done, total: progress.append((done, total)),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.record_id, "review-1")
        self.assertEqual(len(result.reviews), 3)
        self.assertEqual(result.reviews[0].label, LABEL_BEST)
        self.assertEqual(result.reviews[0].loss, 0)
        # 第二手：最佳 400，实战评分 -300 → 损失 700（一般）。
        self.assertEqual(result.reviews[1].label, LABEL_INACCURATE)
        self.assertEqual(result.reviews[1].loss, 700)
        # 第三手：最佳 300，实战评分 -1000 → 损失 1300（失误）。
        self.assertEqual(result.reviews[2].label, LABEL_MISTAKE)
        self.assertGreater(result.reviews[2].loss, 0)
        self.assertEqual(progress[-1], (3, 3))
        self.assertIn("准确率", result.summary_text)
        self.assertEqual(ai.calls, 5)

    def test_cancel_returns_none(self) -> None:
        ai = _ScriptedAI([((7, 7), 0)] * 10)
        result = review_record(
            _record(), ai, cancel=lambda: True
        )
        self.assertIsNone(result)
        self.assertEqual(ai.calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
