"""持续失败的重复提醒。

背景: 哨兵只对 new_failures 告警, 所以一条永不恢复的失败也永不再出声。
2026-08-21 实证: QLD bushfire canary 07-23 坏掉, 响过一次, 之后连续 30 次
运行全红且零告警, 一个州的上游没了一个月没人知道。现在红满 STALE_RED_DAYS
会被重新报一次。
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "score_truth_probes",
    Path(__file__).resolve().parent.parent / "scripts" / "score_truth_probes.py")
probes = importlib.util.module_from_spec(_SPEC)
sys.modules["score_truth_probes"] = probes
_SPEC.loader.exec_module(probes)

DAY = 86400
KEY = "canary|QLD bushfire Tamborine known-positive (QFD BPA proxy)"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """跑 main() 但不碰网络、不发 Telegram, state 落在 tmp。"""
    state = tmp_path / "state.json"
    monkeypatch.setattr(probes, "STATE_FILE", state)
    monkeypatch.setattr(probes, "STALE_RED_DAYS", 7.0)
    monkeypatch.setattr(sys, "argv", ["probes"])

    sent = []
    delivery = {"ok": True}
    fake = type(sys)("alert_telegram")
    fake.send_alert = lambda **kw: (sent.append(kw), delivery["ok"])[1]
    monkeypatch.setitem(sys.modules, "alert_telegram", fake)

    def run(fails, now, deliver=True):
        delivery["ok"] = deliver
        monkeypatch.setattr(probes.time, "time", lambda: now)
        monkeypatch.setattr(probes, "run_canaries", lambda: [
            {"domain": d, "key": k, "status": "FAIL", "note": "dead"}
            for d, k in fails])
        monkeypatch.setattr(probes, "run_anchors", lambda *a, **k: [])
        sent.clear()
        try:
            probes.main()
        except SystemExit as e:
            code = e.code
        return code, list(sent), json.loads(state.read_text())

    return run


def _canary(key=KEY):
    domain, _, name = key.partition("|")
    return (domain, name)


def test_a_still_red_check_is_reported_again_after_a_week(harness):
    t0 = 1_700_000_000.0
    code, sent, _ = harness([_canary()], t0)
    assert code == 1 and len(sent) == 1, "第一天是新失败, 照常告警"
    assert "新失败" in sent[0]["title"]

    # 中间六天: 一直红, 但不该再吵。
    for day in range(1, 7):
        code, sent, _ = harness([_canary()], t0 + day * DAY)
        assert sent == [], f"第 {day} 天不该重复告警"
        assert code == 0, "没有新失败就不该 exit 1"

    code, sent, state = harness([_canary()], t0 + 7 * DAY)
    assert len(sent) == 1, "红满 7 天必须重新报一次"
    assert sent[0]["level"] == "warn", "不是新故障, 用 warn 不是 error"
    assert "持续失败" in sent[0]["title"]
    assert "已红 7 天" in sent[0]["message"]
    assert code == 0, "提醒不该让 cron 再红一次(那会重复推送)"
    assert state["reminded"][KEY] == t0 + 7 * DAY


def test_the_reminder_does_not_repeat_daily(harness):
    t0 = 1_700_000_000.0
    harness([_canary()], t0)
    harness([_canary()], t0 + 7 * DAY)          # 第一次提醒
    for day in (8, 9, 13):
        _code, sent, _ = harness([_canary()], t0 + day * DAY)
        assert sent == [], f"第 {day} 天不该再提醒, 上次提醒才过 {day - 7} 天"
    _code, sent, _ = harness([_canary()], t0 + 14 * DAY)
    assert len(sent) == 1, "距上次提醒又满 7 天, 再报一次"


def test_recovery_clears_the_clock(harness):
    t0 = 1_700_000_000.0
    harness([_canary()], t0)
    _code, _sent, state = harness([], t0 + 3 * DAY)   # 恢复
    assert state["failing"] == [] and state["since"] == {}
    # 三天后又坏: 是新失败, 而且计时从头开始。
    _code, sent, state = harness([_canary()], t0 + 6 * DAY)
    assert len(sent) == 1 and "新失败" in sent[0]["title"]
    assert state["since"][KEY] == t0 + 6 * DAY, "恢复过就不能算连续红"
    _code, sent, _ = harness([_canary()], t0 + 12 * DAY)
    assert sent == [], "距重新失败才 6 天, 还不到提醒线"


def test_a_key_already_red_before_this_shipped_is_stamped_not_invented(harness, tmp_path):
    """老 state 没有 since 字段, 不能把它当成'刚坏'也不能凭空造日期。"""
    t0 = 1_700_000_000.0
    probes.STATE_FILE.write_text(json.dumps({"failing": [KEY], "ts": t0 - 30 * DAY}))
    _code, sent, state = harness([_canary()], t0)
    assert sent == [], "它不是新失败(老 state 里已在 failing), 今天不该告警"
    assert state["since"][KEY] == t0, "没有真实起点就按今天记, 宁可晚报一周"
    _code, sent, _ = harness([_canary()], t0 + 7 * DAY)
    assert len(sent) == 1, "从记账那天起满 7 天, 提醒生效"


def test_new_failures_and_stale_ones_are_separate_messages(harness):
    t0 = 1_700_000_000.0
    old, fresh = _canary(), ("flood", "-27.4390,153.0620")
    harness([old], t0)
    _code, sent, _ = harness([old, fresh], t0 + 7 * DAY)
    assert len(sent) == 2, "一条新失败 + 一条持续失败, 不该混在一起"
    titles = sorted(s["title"] for s in sent)
    assert "新失败" in titles[0] or "新失败" in titles[1]
    stale_msg = [s for s in sent if "持续失败" in s["title"]][0]["message"]
    assert "已红 7 天" in stale_msg and "-27.4390" not in stale_msg, (
        "持续失败那条只该带老的那个 key")


def test_the_twelve_line_cap_says_what_it_dropped(harness):
    """13 条常驻红是现状, 所以摘要第一次发就会撞上这个上限。
    仓库规则: 截断必须说出来, 否则读者会把它当成完整清单。"""
    t0 = 1_700_000_000.0
    many = [("flood", f"-27.{i:04d},153.0") for i in range(15)]
    harness(many, t0)
    _code, sent, _ = harness(many, t0 + 7 * DAY)
    msg = [s for s in sent if "持续失败" in s["title"]][0]["message"]
    assert msg.count("•") == 12, "正文最多 12 行"
    assert "还有 3 项未列出" in msg, "被截掉的必须点名有多少条"
    assert "15 项持续失败" in [s for s in sent if "持续失败" in s["title"]][0]["title"], (
        "标题里的数字必须是真实总数, 不是被截断后的 12")


def test_a_cron_starting_seconds_early_does_not_lose_a_day(harness):
    """窗口按天量, 但 cron 每次开跑的时刻有秒级抖动。上一次提醒记的是那天的
    运行时刻; 七天后若这次跑得早了哪怕 1 秒, 严格 >= 就够不到, 提醒顺延一整天,
    而且下次比较的基准还是老的, 会一直顺延下去。"""
    t0 = 1_700_000_000.0
    harness([_canary()], t0)
    _code, sent, state = harness([_canary()], t0 + 7 * DAY + 40)   # 这天晚 40 秒
    assert len(sent) == 1
    assert state["reminded"][KEY] == t0 + 7 * DAY + 40
    # 再过七天, 这次早了 40 秒: 距上次提醒差 80 秒不足七天。
    _code, sent, _ = harness([_canary()], t0 + 14 * DAY - 40)
    assert len(sent) == 1, "差 80 秒不该把提醒整整推迟一天"


def test_a_dry_run_does_not_spend_the_reminder(harness, monkeypatch):
    """--no-alert 什么都不发, 所以不能把 stale 记成已提醒——那等于用一次
    dry run 换来又一周的沉默, 而真正该收到提醒的人一个字都没看到。"""
    t0 = 1_700_000_000.0
    harness([_canary()], t0)
    monkeypatch.setattr(sys, "argv", ["probes", "--no-alert"])
    _code, sent, state = harness([_canary()], t0 + 7 * DAY)
    assert sent == [], "--no-alert 不该发任何消息"
    assert KEY not in state.get("reminded", {}), "没发出去就不能记成发过"
    # 下一次正常运行必须补上这条提醒。
    monkeypatch.setattr(sys, "argv", ["probes"])
    _code, sent, _ = harness([_canary()], t0 + 7 * DAY + 60)
    assert len(sent) == 1, "dry run 之后的正常运行要把欠下的提醒发出来"


def test_a_dry_run_writes_no_state_at_all(harness, monkeypatch):
    """--no-alert 是调试开关, 只看不改。

    它默认指向 cron 那份 ~/.score_truth_probes_state.json, 而运维 SSH 上去用的
    正是 cron 跑的同一个 ubuntu 账号, 所以文件头自己举的
    `--domain X --no-alert` 例子动的就是生产状态: 一次调试就能把一个真回归
    从"今天 error 告警"无声降级成"七天后 warn 摘要"。"""
    t0 = 1_700_000_000.0
    harness([_canary()], t0)
    before = probes.STATE_FILE.read_text()
    monkeypatch.setattr(sys, "argv", ["probes", "--no-alert"])
    other = ("flood", "-27.4390,153.0620")
    _code, sent, _state = harness([_canary(), other], t0 + DAY)
    assert sent == []
    assert probes.STATE_FILE.read_text() == before, (
        "dry run 一个字节都不该改 state")
    # 回到真实运行: 那条新失败仍然是新的, 照常 error 级报出来。
    monkeypatch.setattr(sys, "argv", ["probes"])
    _code, sent, state = harness([_canary(), other], t0 + 2 * DAY)
    assert len(sent) == 1 and sent[0]["level"] == "error"
    assert f"{other[0]}|{other[1]}" in state["failing"]


def test_a_failed_delivery_does_not_spend_the_reminder(harness):
    """send_alert 送不出去时**返回 False 而不抛异常**, 所以 try/except 看不见它。
    如果照记 reminded, 一次 502 或 token 过期就换来整周沉默——正是这个功能
    要终结的那种沉默, 只是搬到了上一层。"""
    t0 = 1_700_000_000.0
    harness([_canary()], t0)
    _code, sent, state = harness([_canary()], t0 + 7 * DAY, deliver=False)
    assert len(sent) == 1, "确实尝试发了"
    assert KEY not in state.get("reminded", {}), "没送达就不能记成已提醒"
    # 第二天重试, 不必再等一周。
    _code, sent, state = harness([_canary()], t0 + 8 * DAY)
    assert len(sent) == 1, "上次没送达, 下次运行就该重试"
    assert state["reminded"][KEY] == t0 + 8 * DAY


def test_new_failure_lines_do_not_claim_a_red_age(harness):
    """'已红 N 天' 只属于持续失败那条; 新失败今天才红, 标个 0 天是噪音。"""
    t0 = 1_700_000_000.0
    _code, sent, _ = harness([_canary()], t0)
    assert "已红" not in sent[0]["message"], sent[0]["message"]


def test_a_scoped_domain_run_does_not_reset_other_domains(harness, monkeypatch):
    """--domain 只探一个域, 其余域这次是'未知'不是'已恢复'。
    整份覆写会让它们下次变成假的新失败, 而且 since 归零, 计时前功尽弃。"""
    t0 = 1_700_000_000.0
    other = ("walkability", "-42.86412,147.35853")
    harness([_canary(), other], t0)

    monkeypatch.setattr(sys, "argv", ["probes", "--domain", "canary"])
    _code, _sent, state = harness([_canary()], t0 + 1 * DAY)
    okey = f"{other[0]}|{other[1]}"
    assert okey in state["failing"], "没探到不等于恢复了"
    assert state["since"][okey] == t0, "别人的计时不能被清零"

    # 回到全量运行: 它不该被当成新失败。
    monkeypatch.setattr(sys, "argv", ["probes"])
    _code, sent, _ = harness([_canary(), other], t0 + 2 * DAY)
    assert sent == [], "两条都是老的, 不该报新失败"


def test_a_canary_that_recovers_during_a_scoped_run_is_cleared(harness, monkeypatch):
    """run_canaries 不看 --domain, 每次都全跑, 所以 canary 永远是实测过的。
    把它当成'本次没探到'而保留下来, 会让一条已经恢复的 canary 永远挂在 failing 里。"""
    t0 = 1_700_000_000.0
    other = ("walkability", "-42.86412,147.35853")
    harness([_canary(), other], t0)

    # --domain walkability: canary 仍然被探(且这次是绿的), walkability 也被探。
    monkeypatch.setattr(sys, "argv", ["probes", "--domain", "walkability"])
    _code, _sent, state = harness([other], t0 + DAY)
    assert KEY not in state["failing"], "canary 恢复了就该从 failing 里消失"
    assert KEY not in state["since"], "计时也该一并清掉"
    assert f"{other[0]}|{other[1]}" in state["failing"], "walkability 这次仍是红的"


def test_an_undelivered_new_failure_is_retried_next_run(harness):
    """error 那条比 warn 更紧急, 不能反而只享受更弱的保障。
    送不出去就别写进 state, 下次运行它仍是'新失败', 照 error 级重报。"""
    t0 = 1_700_000_000.0
    _code, sent, state = harness([_canary()], t0, deliver=False)
    assert len(sent) == 1 and sent[0]["level"] == "error"
    assert state["failing"] == [], "没送达就不能记成已知, 否则永远等不到重报"
    assert state["since"] == {}, "计时也不能起, 它还没被任何人看到过"
    _code, sent, state = harness([_canary()], t0 + DAY)
    assert len(sent) == 1 and sent[0]["level"] == "error", "下次运行必须重报"
    assert state["failing"] == [KEY]


def test_a_delivered_new_failure_is_recorded_normally(harness):
    """反向对照: 送达了就照常记账, 不能因为上面那条把正常路径也拖坏。"""
    t0 = 1_700_000_000.0
    _code, sent, state = harness([_canary()], t0)
    assert len(sent) == 1 and state["failing"] == [KEY]
    _code, sent, _ = harness([_canary()], t0 + DAY)
    assert sent == [], "已经报过就不该再报"


def test_new_failure_lines_are_ordered_deterministically(harness):
    """keys 是 set, 直接切片会按 hash 序: state 丢失时 13 条全成新失败,
    被丢掉的是随机一条, 而且每次运行行序都在变。"""
    t0 = 1_700_000_000.0
    many = [("flood", f"-27.{i:04d},153.0") for i in range(15)]
    _code, sent, _ = harness(many, t0)
    msg = sent[0]["message"]
    keys_in_order = [ln.split(": ")[0].split()[-1] for ln in msg.splitlines()
                     if ln.startswith("•")]
    assert keys_in_order == sorted(keys_in_order), "行序必须稳定可预期"


def test_the_state_write_is_atomic(harness, monkeypatch, tmp_path):
    """state 现在还扛着计时, 写坏一次等于把'已经红了一个月'清零重来。"""
    t0 = 1_700_000_000.0
    harness([_canary()], t0)
    replaced = []
    real_replace = probes.os.replace
    monkeypatch.setattr(probes.os, "replace",
                        lambda a, b: replaced.append((str(a), str(b))) or real_replace(a, b))
    harness([_canary()], t0 + DAY)
    assert replaced, "必须走 os.replace 而不是直接覆写目标文件"
    src, dst = replaced[-1]
    assert src.endswith(".tmp") and dst == str(probes.STATE_FILE)
    # cron wrapper 不加 flock, 所以两个重叠运行不能共用同一个 tmp 名:
    # 一个 rename 走另一个写到一半的文件, 而读路径会静默吞掉这份垃圾。
    assert str(os.getpid()) in src, f"tmp 名里要带 pid 才不会互踩: {src}"


def test_the_truncation_notice_points_at_a_path_that_holds_the_log(harness):
    """cron 在 /var/www/property-scores 下跑, 那里**有** logs/ 但里面没有这个文件,
    所以相对路径会把人送进一个真实但空的目录, 读起来像'日志没了'。
    这行字出现的时机恰恰是有人被告知'还有 N 项没列出'的时候, 最不该指错。

    ⚠️ 断言的是**默认值**。第一版这里 monkeypatch 了 LOG_PATH 再断言同一个字符串,
    等于在验证我自己刚设进去的值 —— 变异回相对路径时测试照样绿。
    """
    assert probes.LOG_PATH.startswith("/"), (
        f"默认必须是绝对路径, 现在是 {probes.LOG_PATH!r}")
    assert probes.LOG_PATH.endswith("/truth_probes.log")
    t0 = 1_700_000_000.0
    many = [("flood", f"-27.{i:04d},153.0") for i in range(15)]
    harness(many, t0)
    _code, sent, _ = harness(many, t0 + 7 * DAY)
    msg = [s for s in sent if "持续失败" in s["title"]][0]["message"]
    assert probes.LOG_PATH in msg, msg


