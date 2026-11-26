"""LocaleLens CLI。"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from . import ensure_dirs, load_sources, now
from .adapters import NothingToDo, get_adapters
from .checks import run_checks
from .digest import build_daily
from .store import health_summary, last_ok_ts, log_health, write_day


def cmd_run(args: argparse.Namespace) -> int:
    ensure_dirs()
    adapters = get_adapters()

    if args.source:
        if args.source not in adapters:
            print(f"未知源：{args.source}")
            print("可用源：" + ", ".join(adapters))
            return 2
        selected = {args.source: adapters[args.source]}
    else:
        selected = dict(adapters)

    for ex in args.exclude or []:
        selected.pop(ex, None)

    touched_days: set[str] = set()
    cfg_map = {s["id"]: s for s in load_sources().get("sources", [])}

    for sid, adapter in selected.items():
        # 频率守卫：低频源（如 POI，一周一次）不能在每日 run 里被反复触发
        min_h = float(cfg_map.get(sid, {}).get("min_interval_hours", 0) or 0)
        if min_h > 0 and not args.force:
            last = last_ok_ts(sid)
            if last is not None:
                elapsed = (now() - last).total_seconds() / 3600.0
                if elapsed < min_h:
                    print(f"→ {sid}\n  ○ 跳过（距上次成功 {elapsed:.1f}h，需间隔 {min_h:.0f}h）")
                    continue

        print(f"→ {sid}")
        try:
            observations = adapter.run(backfill=args.backfill)
        except NothingToDo as exc:
            log_health(source_id=sid, dataset="*", ok=True, rows=0)
            print(f"  ○ 无新增（{exc}）")
            continue
        except Exception as exc:  # noqa: BLE001
            log_health(source_id=sid, dataset="*", ok=False, error=str(exc)[:500])
            print(f"  ✗ 抓取失败：{exc}")
            continue

        if not observations:
            log_health(source_id=sid, dataset="*", ok=False, error="解析出 0 条记录")
            print("  ✗ 解析出 0 条记录（页面结构可能已变）")
            continue

        grouped: dict[str, list] = defaultdict(list)
        for obs in observations:
            grouped[obs.dataset].append(obs)

        for dataset, rows in sorted(grouped.items()):
            day = rows[0].fetched_at[:10]
            path, total = write_day(rows, source_id=sid, dataset=dataset, day=day)
            log_health(source_id=sid, dataset=dataset, ok=True, rows=len(rows))
            touched_days.add(day)
            print(f"  ✓ {dataset}: {len(rows)} 条 → {path.name}（当日累计 {total} 行）")

    for day in sorted(touched_days):
        p = build_daily(day)
        print(f"→ 摘要：{p}")

    findings = run_checks()
    if findings:
        print(f"→ 数据检查：发现 {len(findings)} 条口径/修订问题（详见摘要第七节）")

    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    ensure_dirs()
    day = args.day or now().strftime("%Y-%m-%d")
    p = build_daily(day)
    print(p)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    findings = run_checks()
    if not findings:
        print("未发现口径或修订问题。")
        return 0
    for f in findings:
        print(f"[{f['severity']:<6}] {f['message']}")
    print(f"\n共 {len(findings)} 条发现，已写入 derived/checks/")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    from . import CONFIG, ROOT, get_secret, load_sources

    env_path = ROOT / ".env"
    print(f".env: {'已存在' if env_path.exists() else '不存在'}")
    print()
    print("密钥状态：")
    for src in load_sources().get("sources", []):
        name = src.get("requires_secret")
        if not name:
            continue
        val = get_secret(name)
        if val:
            masked = val[:4] + "*" * max(0, len(val) - 8) + val[-4:] if len(val) > 8 else "****"
            print(f"  ✓ {name:<12} 已配置（{masked}）→ {src['id']}")
        else:
            print(f"  ✗ {name:<12} 未配置 → {src['id']} 会自动跳过")
    print()
    print("源清单：")
    for src in load_sources().get("sources", []):
        flag = "启用" if src.get("enabled", True) else "停用"
        print(f"  [{flag}] {src['id']:<28}{src.get('name', '')}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    hs = health_summary()
    if not hs:
        print("暂无健康度记录。先跑一次 run。")
        return 0
    print(f"{'源':<32}{'数据集':<24}{'最近成功':<21}{'连败':>4}{'失败率':>8}")
    for h in hs:
        print(
            f"{h['source_id']:<32}{h['dataset']:<24}"
            f"{(h['last_ok'] or '从未')[:19]:<21}{h['consecutive_fails']:>4}"
            f"{(h['fail_rate'] if h['fail_rate'] is not None else '-'):>8}"
        )
        if h["last_error"]:
            print(f"    最近错误：{h['last_error'][:160]}")
    return 0


def cmd_days(args: argparse.Namespace) -> int:
    from .store import available_days

    cfg = load_sources()
    for src in cfg["sources"]:
        if not src.get("datasets"):
            continue
        for ds in src["datasets"]:
            days = available_days(source_id=src["id"], dataset=ds["key"])
            print(f"{src['id']} / {ds['key']}: {len(days)} 天" + (f"（{days[0]} → {days[-1]}）" if days else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="locale-lens", description="LocaleLens 地区数据观察站")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="抓取并落盘")
    p_run.add_argument("--source", help="只跑指定源")
    p_run.add_argument(
        "--exclude",
        action="append",
        metavar="SOURCE_ID",
        help="排除某个源（可重复），例如本地跑统计局时排除它、云端跑其余",
    )
    p_run.add_argument(
        "--force",
        action="store_true",
        help="忽略 min_interval_hours 频率守卫，强制执行",
    )
    p_run.add_argument(
        "--backfill",
        action="store_true",
        help="回填历史（列表型源会翻完所有分页），日常不必用",
    )
    p_run.set_defaults(func=cmd_run)

    p_dig = sub.add_parser("digest", help="生成每日摘要")
    p_dig.add_argument("--day", help="YYYY-MM-DD，默认今天")
    p_dig.set_defaults(func=cmd_digest)

    p_h = sub.add_parser("health", help="源健康度")
    p_h.set_defaults(func=cmd_health)

    p_c = sub.add_parser("check", help="数据质量检查（口径变更 / 修订）")
    p_c.set_defaults(func=cmd_check)

    p_cfg = sub.add_parser("config", help="查看密钥与源配置状态")
    p_cfg.set_defaults(func=cmd_config)

    p_d = sub.add_parser("days", help="已积累的天数")
    p_d.set_defaults(func=cmd_days)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
