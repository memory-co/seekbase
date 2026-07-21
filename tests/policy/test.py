"""policy — 能力 × 策略授权(operator-registry §6):deny > allow > 模式缺省,
编译期拒绝(管道不启动)。See README.md."""
from __future__ import annotations

import pytest

from seekbase import Cap, PermissionDenied, Policy
from seekbase.operator import builtin_operators
from tests.conftest import FakeEmbedder, SCHEMA, open_db


def _op(name):
    return next(o for o in builtin_operators() if o.name == name)


# ─── 单元:决策顺序 deny > allow > 模式 ─────────────────────────────────

def test_read_only_denies_exec():
    with pytest.raises(PermissionDenied):
        Policy().check(_op("sh"))


def test_sandboxed_allows_exec():
    Policy(mode="sandboxed").check(_op("sh"))          # no raise


def test_deny_beats_mode():
    with pytest.raises(PermissionDenied):
        Policy(mode="trusted", deny=("sh",)).check(_op("sh"))


def test_deny_caps_beats_mode():
    with pytest.raises(PermissionDenied):
        Policy(mode="trusted", deny_caps=(Cap.EXEC,)).check(_op("jq"))


def test_allowlist_restricts():
    p = Policy(allow=("search", "scan"))
    p.check(_op("search"))
    with pytest.raises(PermissionDenied):
        p.check(_op("grep"))                           # PURE but not in allowlist


def test_unknown_mode_rejected():
    with pytest.raises(PermissionDenied):
        Policy(mode="yolo")


# ─── 集成:编译期拒,管道不启动 ────────────────────────────────────────

async def test_sh_denied_by_default(db):
    with pytest.raises(PermissionDenied):
        await db.query("scan cards | sh 'cat'")


async def test_sh_runs_under_sandboxed(tmp_path):
    db = await open_db(tmp_path, embedder=FakeEmbedder())
    await db.close()
    from seekbase import Seekbase
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA, embedder=FakeEmbedder(),
                             policy=Policy(mode="sandboxed"))
    try:
        await db.wait(await db.insert("cards", [
            {"card_id": "c1", "issue": "pty ERROR here", "kind": "bug", "n": 1},
            {"card_id": "c2", "issue": "all fine", "kind": "ok", "n": 2},
        ]))
        rows = await db.query("scan cards | sh 'grep ERROR' | SELECT card_id FROM _in")
        assert rows == [{"card_id": "c1"}]
    finally:
        await db.close()


async def test_denylist_survives_escalation(tmp_path):
    from seekbase import Seekbase
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA, embedder=FakeEmbedder(),
                             policy=Policy(mode="trusted", deny=("sh",)))
    try:
        with pytest.raises(PermissionDenied):
            await db.query("scan cards | sh 'cat'")
    finally:
        await db.close()


async def test_permission_denied_survives_http(pair):
    """错误过线保型:server 侧 PermissionDenied → client 侧还是它(403)。"""
    _, client = pair
    with pytest.raises(PermissionDenied):
        await client.query("scan cards | sh 'cat'")
