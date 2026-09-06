#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抽样方案管理器（CRUD + 抽样记录追踪）
=====================================

用途：管理检验抽样方案的创建、查看、更新、删除，并为每次方案变更自动生成
      可追溯的抽样记录。方案与记录均为 JSON 文件，便于人工审阅与审计追溯。

用法：
    python scripts/scheme_manager.py create --name <方案名> \
        --type <simple|stratified|systematic|cluster> --params <JSON参数>
    python scripts/scheme_manager.py list
    python scripts/scheme_manager.py get  --name <方案名>
    python scripts/scheme_manager.py update --name <方案名> --params <JSON参数>
    python scripts/scheme_manager.py delete --name <方案名>
    python scripts/scheme_manager.py record --id <记录ID>

--params 支持的参数：
    sample_size    样本量（正整数），与 sample_rate 二选一
    sample_rate    采样比例（0~1 浮点），与 sample_size 二选一
    strata_field   分层字段（stratified 分层抽样必需）
    interval       抽样间隔（systematic 系统抽样必需，正整数）
    cluster_field  整群字段（cluster 整群抽样必需）
    另支持 sampler.py 已实现的扩展参数：allocation（分层分配方式，
    proportional/equal）、random_start（系统抽样起点）、cluster_rate（抽群比例）。

存储约定（与同技能其他脚本对齐）：
    方案主存储   ：./sampling_schemes.json（可用 --store 覆盖）
    方案镜像目录 ：./sampling_schemes/<方案名>.json
                   —— sampler.py 的 load_scheme() 正是从该目录逐个读取方案文件，
                      因此每次方案变更都会同步写入镜像，保证 sampler.py 可直接消费。
                   —— 若主存储不存在而镜像目录有方案，会自动反向导入主存储。
    抽样记录目录 ：./sampling_records/（可用 --records-dir 覆盖）
                  每次 create/update/delete 均生成一条操作记录，
                  记录内容包含：方案参数、抽样时间、数据摘要、样本索引、操作日志。
                  sampler.py 带 --record 执行时生成的记录同样落在该目录，
                  record --id 对两种记录都能读取。

输出：所有命令均向 stdout 输出 JSON 对象（ensure_ascii=False），便于智能体解析。

退出码约定：
    0 = 成功
    1 = 参数错误或输入文件不存在（方案不存在、参数校验失败、JSON 解析失败）
    2 = 处理失败（存储文件损坏且无法恢复、写入失败等）
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime

# Windows 控制台默认 GBK，强制切到 UTF-8，避免中文输出报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 本脚本纯标准库实现，无第三方依赖

# 默认存储路径（相对当前工作目录，与 SKILL.md 一致）
DEFAULT_STORE = "./sampling_schemes.json"
DEFAULT_SCHEMES_DIR = "./sampling_schemes"
DEFAULT_RECORDS_DIR = "./sampling_records"

# 支持的抽样类型及其中文名
SCHEME_TYPES = {
    "simple": "简单随机抽样",
    "stratified": "分层抽样",
    "systematic": "系统抽样",
    "cluster": "整群抽样",
}

# 各类型必需参数（用于创建时校验）
REQUIRED_PARAMS = {
    "simple": [],
    "stratified": ["strata_field"],
    "systematic": ["interval"],
    "cluster": ["cluster_field"],
}

# 可选扩展参数（sampler.py 支持）
OPTIONAL_PARAMS = ["sample_size", "sample_rate", "allocation",
                   "random_start", "cluster_rate", "strata_field",
                   "interval", "cluster_field"]


# ---------------- 基础工具 ----------------
def now_text():
    """当前时间字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def gen_record_id():
    """
    生成记录 ID。
    格式与 sampler.py 的 generate_record_id() 保持一致：
    REC + 年月日时分秒 + 2 位随机数，便于统一检索。
    """
    return "REC{}{:02d}".format(datetime.now().strftime("%Y%m%d%H%M%S"),
                                random.randint(10, 99))


def out(data, exit_code=0):
    """统一输出 JSON 并退出"""
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def fail(message, exit_code=1):
    """统一错误输出"""
    out({"success": False, "error": message}, exit_code)


def load_params(raw):
    """
    解析 --params。
    支持：JSON 字符串 / JSON 文件路径 / key=value 形式的简写（如 sample_size=100）。
    """
    if raw is None or str(raw).strip() == "":
        return {}

    text = str(raw).strip()

    # 1) 文件路径
    if os.path.isfile(text):
        try:
            with open(text, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            fail("参数文件 JSON 解析失败: {}".format(exc))
        if not isinstance(data, dict):
            fail("参数文件内容必须是 JSON 对象")
        return data

    # 2) JSON 字符串
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            fail("--params JSON 解析失败: {}（line {} column {}）".format(
                exc.msg, exc.lineno, exc.colno))
        if not isinstance(data, dict):
            fail("--params 必须是 JSON 对象")
        return data

    # 3) 简写 k=v,k=v
    if "=" in text:
        result = {}
        for item in text.split(","):
            if "=" not in item:
                fail("--params 简写格式错误，应为 key=value，收到: {}".format(item))
            key, _, value = item.partition("=")
            key, value = key.strip(), value.strip()
            # 自动识别数字与布尔
            low = value.lower()
            if low in ("true", "false"):
                result[key] = (low == "true")
            else:
                try:
                    result[key] = int(value)
                except ValueError:
                    try:
                        result[key] = float(value)
                    except ValueError:
                        result[key] = value
        return result

    fail("--params 无法解析：请传入 JSON 字符串、JSON 文件路径或 key=value 简写")


# ---------------- 存储读写 ----------------
def load_store(store_path, schemes_dir):
    """
    读取方案主存储。
    返回 dict：{"version":1, "updated_at":..., "schemes":{名称: 方案对象}}
    主存储不存在时，尝试从镜像目录 ./sampling_schemes/*.json 反向导入。
    """
    if os.path.isfile(store_path):
        try:
            with open(store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            fail("方案存储文件解析失败，请修复或删除后重试: {}\n错误: {}".format(store_path, exc), 2)
        except OSError as exc:
            fail("方案存储文件读取失败: {}".format(exc), 2)

        # 兼容旧格式：顶层直接就是 {方案名: 方案对象}
        if isinstance(data, dict) and "schemes" in data and isinstance(data["schemes"], dict):
            data.setdefault("version", 1)
            return data
        if isinstance(data, dict):
            return {"version": 1, "updated_at": data.get("updated_at", ""), "schemes": data}
        fail("方案存储文件格式不正确（应为 JSON 对象）: {}".format(store_path), 2)

    # 主存储不存在 → 从镜像目录反向导入
    schemes = {}
    if os.path.isdir(schemes_dir):
        for filename in sorted(os.listdir(schemes_dir)):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(schemes_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    item = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue  # 跳过损坏的镜像文件，不阻断整体加载
            if isinstance(item, dict) and item.get("name"):
                schemes[item["name"]] = item

    return {"version": 1, "updated_at": "", "schemes": schemes}


def save_store(store_path, store):
    """写入方案主存储"""
    store["version"] = 1
    store["updated_at"] = now_text()
    try:
        parent = os.path.dirname(os.path.abspath(store_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(store_path, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        fail("方案存储文件写入失败: {}".format(exc), 2)


def mirror_scheme(schemes_dir, scheme):
    """
    把单个方案同步写入镜像目录 ./sampling_schemes/<方案名>.json。
    sampler.py 的 load_scheme() 依赖该文件，必须保持同步。
    """
    try:
        os.makedirs(schemes_dir, exist_ok=True)
        path = os.path.join(schemes_dir, "{}.json".format(scheme["name"]))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scheme, f, ensure_ascii=False, indent=2)
        return path
    except OSError as exc:
        fail("方案镜像文件写入失败: {}".format(exc), 2)


def remove_mirror(schemes_dir, name):
    """删除方案时同步清理镜像文件"""
    path = os.path.join(schemes_dir, "{}.json".format(name))
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass  # 镜像清理失败不影响主流程


# ---------------- 参数校验 ----------------
def validate_params(scheme_type, params, partial=False):
    """
    校验方案参数。
    partial=True 时（update 场景）只校验传入的字段，不强制必需项。
    返回规范化后的参数副本。
    """
    if not isinstance(params, dict):
        fail("--params 必须是 JSON 对象")

    if scheme_type not in SCHEME_TYPES:
        fail("不支持的抽样类型: {}，可选值为 {}".format(
            scheme_type, "/".join(SCHEME_TYPES.keys())))

    cleaned = dict(params)

    # 未知参数提醒（不阻断，仅提示，避免 sampler.py 扩展参数被误判）
    unknown = [k for k in cleaned if k not in OPTIONAL_PARAMS]
    if unknown:
        print("[提示] 以下参数不在已知清单中，将原样保存: {}".format("、".join(unknown)),
              file=sys.stderr)

    # 数值类参数校验
    if "sample_size" in cleaned:
        try:
            size = int(cleaned["sample_size"])
        except (TypeError, ValueError):
            fail("sample_size 必须是整数，当前值: {}".format(cleaned["sample_size"]))
        if size < 1:
            fail("sample_size 必须 ≥ 1，当前值: {}".format(size))
        cleaned["sample_size"] = size

    if "sample_rate" in cleaned:
        try:
            rate = float(cleaned["sample_rate"])
        except (TypeError, ValueError):
            fail("sample_rate 必须是数字，当前值: {}".format(cleaned["sample_rate"]))
        if not 0 < rate <= 1:
            fail("sample_rate 必须大于 0 且不超过 1，当前值: {}".format(rate))
        cleaned["sample_rate"] = rate

    if "interval" in cleaned:
        try:
            interval = int(cleaned["interval"])
        except (TypeError, ValueError):
            fail("interval 必须是整数，当前值: {}".format(cleaned["interval"]))
        if interval < 1:
            fail("interval 必须 ≥ 1，当前值: {}".format(interval))
        cleaned["interval"] = interval

    if "cluster_rate" in cleaned:
        rate = float(cleaned["cluster_rate"])
        if not 0 < rate <= 1:
            fail("cluster_rate 必须大于 0 且不超过 1，当前值: {}".format(rate))
        cleaned["cluster_rate"] = rate

    if "allocation" in cleaned and cleaned["allocation"] not in ("proportional", "equal"):
        fail("allocation 只支持 proportional（比例分配）或 equal（等额分配）")

    # 必需参数校验（创建时强制）
    if not partial:
        for key in REQUIRED_PARAMS.get(scheme_type, []):
            if not cleaned.get(key):
                fail("{}（{}）缺少必需参数: {}".format(
                    scheme_type, SCHEME_TYPES[scheme_type], key))

        # 简单随机/分层抽样需要确定样本量口径
        if scheme_type in ("simple", "stratified"):
            if not cleaned.get("sample_size") and not cleaned.get("sample_rate"):
                fail("{}（{}）必须指定 sample_size 或 sample_rate 其中之一".format(
                    scheme_type, SCHEME_TYPES[scheme_type]))
            if cleaned.get("sample_size") and cleaned.get("sample_rate"):
                print("[提示] 同时传入 sample_size 与 sample_rate 时，"
                      "sampler.py 优先使用 sample_size。", file=sys.stderr)

    return cleaned


def describe_params(scheme_type, params):
    """把参数翻译成一句人话，用于 list / get 输出"""
    bits = []
    if params.get("sample_size"):
        bits.append("样本量 {}".format(params["sample_size"]))
    if params.get("sample_rate"):
        bits.append("采样比例 {}".format(params["sample_rate"]))
    if params.get("strata_field"):
        bits.append("分层字段 {}".format(params["strata_field"]))
    if params.get("interval"):
        bits.append("抽样间隔 {}".format(params["interval"]))
    if params.get("cluster_field"):
        bits.append("整群字段 {}".format(params["cluster_field"]))
    if params.get("allocation"):
        bits.append("分配方式 {}".format(params["allocation"]))
    if params.get("cluster_rate"):
        bits.append("抽群比例 {}".format(params["cluster_rate"]))
    return "{}（{}）".format(SCHEME_TYPES.get(scheme_type, scheme_type),
                          "，".join(bits) if bits else "无参数")


# ---------------- 抽样记录 ----------------
def write_record(records_dir, action, scheme=None, old_scheme=None, note=""):
    """
    生成一条抽样/操作记录，落盘到 ./sampling_records/<record_id>.json。
    记录内容按 SKILL.md 要求包含：方案参数、抽样时间、数据摘要、样本索引、操作日志。
    """
    record_id = gen_record_id()
    timestamp = now_text()

    if scheme:
        scheme_name = scheme.get("name", "")
        scheme_type = scheme.get("type", "")
        scheme_params = scheme.get("params", {})
    else:
        scheme_name = (old_scheme or {}).get("name", "")
        scheme_type = (old_scheme or {}).get("type", "")
        scheme_params = (old_scheme or {}).get("params", {})

    record = {
        "record_id": record_id,
        "action": action,
        "scheme_name": scheme_name,
        "scheme_type": scheme_type,
        "sampling_time": timestamp,
        "created_at": datetime.now().isoformat(),
        "operator": "scheme_manager",
        "scheme_params": scheme_params,
        "data_summary": {
            "scheme": describe_params(scheme_type, scheme_params),
            "source": note or "方案管理操作（{}）".format(action),
            "note": "本记录由 scheme_manager.py 在方案变更时自动生成；"
                    "实际抽样结果记录由 sampler.py --record 生成，落在本目录下。",
        },
        "sample_indexes": [],
        "status": "scheme_operation",
        "log": [
            {"time": timestamp, "action": action,
             "detail": "{}方案「{}」".format(
                 {"create": "创建", "update": "更新", "delete": "删除"}.get(action, action),
                 scheme_name)},
        ],
    }

    if action == "update" and old_scheme:
        record["log"].append({
            "time": timestamp,
            "action": "params_changed",
            "detail": "原参数：{} → 新参数：{}".format(
                json.dumps(old_scheme.get("params", {}), ensure_ascii=False),
                json.dumps(scheme_params, ensure_ascii=False)),
        })

    try:
        os.makedirs(records_dir, exist_ok=True)
        path = os.path.join(records_dir, "{}.json".format(record_id))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        # 记录写入失败不阻断方案管理主流程，但要明确告知
        print("[警告] 抽样记录写入失败: {}".format(exc), file=sys.stderr)
        return record_id, None

    return record_id, path


def find_record(records_dir, record_id):
    """
    查找记录文件。
    支持完整 ID（REC2024090212000012）与带 .json 后缀的写法。
    返回文件路径或 None。
    """
    if not os.path.isdir(records_dir):
        return None
    name = record_id if record_id.endswith(".json") else record_id + ".json"
    direct = os.path.join(records_dir, name)
    if os.path.isfile(direct):
        return direct
    # 兜底：按 ID 前缀模糊匹配，便于只记得部分 ID 时检索
    for filename in sorted(os.listdir(records_dir)):
        if filename.startswith(record_id):
            return os.path.join(records_dir, filename)
    return None


# ---------------- 子命令实现 ----------------
def cmd_create(args):
    """创建抽样方案"""
    store = load_store(args.store, args.schemes_dir)
    name = args.name.strip()

    if not name:
        fail("方案名不能为空")
    if name in store["schemes"]:
        fail("方案已存在: {}（如需修改请使用 update）".format(name))

    scheme_type = args.type.strip().lower()
    params = validate_params(scheme_type, load_params(args.params))

    scheme = {
        "name": name,
        "type": scheme_type,
        "params": params,
        "description": args.description or "",
        "created_at": now_text(),
        "updated_at": now_text(),
    }

    store["schemes"][name] = scheme
    save_store(args.store, store)
    mirror_path = mirror_scheme(args.schemes_dir, scheme)
    record_id, record_path = write_record(args.records_dir, "create", scheme=scheme)

    out({
        "success": True,
        "action": "create",
        "scheme": scheme,
        "summary": describe_params(scheme_type, params),
        "store": os.path.abspath(args.store),
        "mirror": os.path.abspath(mirror_path),
        "record_id": record_id,
        "record_path": os.path.abspath(record_path) if record_path else None,
    })


def cmd_list(args):
    """列出全部抽样方案"""
    store = load_store(args.store, args.schemes_dir)
    schemes = store["schemes"]

    items = []
    for name in sorted(schemes):
        scheme = schemes[name]
        items.append({
            "name": name,
            "type": scheme.get("type", ""),
            "type_cn": SCHEME_TYPES.get(scheme.get("type", ""), ""),
            "params": scheme.get("params", {}),
            "summary": describe_params(scheme.get("type", ""), scheme.get("params", {})),
            "updated_at": scheme.get("updated_at", ""),
        })

    out({
        "success": True,
        "action": "list",
        "count": len(items),
        "schemes": items,
        "store": os.path.abspath(args.store),
        "schemes_dir": os.path.abspath(args.schemes_dir),
        "hint": "使用 sampler.py generate --scheme <方案名> 执行抽样",
    })


def cmd_get(args):
    """查看单个方案详情"""
    store = load_store(args.store, args.schemes_dir)
    name = args.name.strip()
    if name not in store["schemes"]:
        fail("方案不存在: {}（可用 list 查看已有方案）".format(name))

    scheme = store["schemes"][name]
    out({
        "success": True,
        "action": "get",
        "scheme": scheme,
        "summary": describe_params(scheme.get("type", ""), scheme.get("params", {})),
        "usage": "python scripts/sampler.py generate --data <解析后数据.json> "
                 "--scheme {} --output <抽样结果.json> --record".format(name),
    })


def cmd_update(args):
    """更新方案参数（增量合并）"""
    store = load_store(args.store, args.schemes_dir)
    name = args.name.strip()
    if name not in store["schemes"]:
        fail("方案不存在: {}（可用 list 查看已有方案）".format(name))

    scheme = store["schemes"][name]
    old_scheme = json.loads(json.dumps(scheme, ensure_ascii=False))  # 深拷贝留档

    incoming = load_params(args.params)
    if not incoming:
        fail("update 需要提供 --params，且内容不能为空")

    # 以原参数为基础增量合并
    merged = dict(scheme.get("params", {}))
    merged.update(incoming)

    scheme_type = args.type.strip().lower() if args.type else scheme.get("type", "")
    if args.type:
        scheme["type"] = scheme_type

    # partial=True：只校验传入字段；但要保证合并后整体仍满足必需项
    merged = validate_params(scheme_type, merged, partial=True)
    missing = [k for k in REQUIRED_PARAMS.get(scheme_type, []) if not merged.get(k)]
    if missing:
        fail("更新后方案缺少必需参数: {}（类型 {}）".format("、".join(missing), scheme_type))
    if scheme_type in ("simple", "stratified") and not merged.get("sample_size") \
            and not merged.get("sample_rate"):
        fail("更新后方案必须保留 sample_size 或 sample_rate 其中之一")

    scheme["params"] = merged
    scheme["updated_at"] = now_text()
    if args.description:
        scheme["description"] = args.description

    store["schemes"][name] = scheme
    save_store(args.store, store)
    mirror_path = mirror_scheme(args.schemes_dir, scheme)
    record_id, record_path = write_record(
        args.records_dir, "update", scheme=scheme, old_scheme=old_scheme)

    out({
        "success": True,
        "action": "update",
        "scheme": scheme,
        "summary": describe_params(scheme_type, merged),
        "changed_keys": sorted(incoming.keys()),
        "mirror": os.path.abspath(mirror_path),
        "record_id": record_id,
        "record_path": os.path.abspath(record_path) if record_path else None,
    })


def cmd_delete(args):
    """删除抽样方案"""
    store = load_store(args.store, args.schemes_dir)
    name = args.name.strip()
    if name not in store["schemes"]:
        fail("方案不存在: {}（可用 list 查看已有方案）".format(name))

    removed = store["schemes"].pop(name)
    save_store(args.store, store)
    remove_mirror(args.schemes_dir, name)
    record_id, record_path = write_record(
        args.records_dir, "delete", old_scheme=removed, note="删除方案「{}」".format(name))

    out({
        "success": True,
        "action": "delete",
        "deleted": name,
        "remaining": len(store["schemes"]),
        "record_id": record_id,
        "record_path": os.path.abspath(record_path) if record_path else None,
    })


def cmd_record(args):
    """查看抽样/操作记录详情"""
    path = find_record(args.records_dir, args.id.strip())
    if not path:
        fail("记录不存在: {}（记录目录: {}）".format(
            args.id, os.path.abspath(args.records_dir)))

    try:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except json.JSONDecodeError as exc:
        fail("记录文件解析失败: {}".format(exc), 2)
    except OSError as exc:
        fail("记录文件读取失败: {}".format(exc), 2)

    out({
        "success": True,
        "action": "record",
        "record_id": record.get("record_id", os.path.basename(path).replace(".json", "")),
        "record_path": os.path.abspath(path),
        "record": record,
    })


# ---------------- 命令行入口 ----------------
def build_parser():
    """构建子命令解析器"""
    parser = argparse.ArgumentParser(
        description="抽样方案管理器（create/list/get/update/delete/record）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/scheme_manager.py create --name plan_a --type simple "
               "--params '{\"sample_size\": 500}'\n"
               "  python scripts/scheme_manager.py create --name plan_b --type stratified "
               "--params '{\"strata_field\":\"产品类别\",\"sample_size\":150}'\n"
               "  python scripts/scheme_manager.py list\n"
               "  python scripts/scheme_manager.py get --name plan_a\n"
               "  python scripts/scheme_manager.py update --name plan_a "
               "--params '{\"sample_size\": 800}'\n"
               "  python scripts/scheme_manager.py delete --name plan_b\n"
               "  python scripts/scheme_manager.py record --id REC2024090212000012")

    # 全局选项：放在子命令之前（如 scheme_manager.py --store x.json list）
    parser.add_argument("--store", default=DEFAULT_STORE,
                        help="方案主存储 JSON 文件路径（默认 ./sampling_schemes.json）")
    parser.add_argument("--schemes-dir", default=DEFAULT_SCHEMES_DIR,
                        help="方案镜像目录，供 sampler.py 读取（默认 ./sampling_schemes）")
    parser.add_argument("--records-dir", default=DEFAULT_RECORDS_DIR,
                        help="抽样记录目录（默认 ./sampling_records）")

    # 公共选项：既支持放在子命令之前（scheme_manager.py --store x.json list），
    # 也支持放在子命令之后（scheme_manager.py list --store x.json）。
    # 用 SUPPRESS 避免子命令未传参时用 None 覆盖顶层已解析的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=argparse.SUPPRESS,
                        help="方案主存储 JSON 文件路径（默认 ./sampling_schemes.json）")
    common.add_argument("--schemes-dir", default=argparse.SUPPRESS,
                        help="方案镜像目录，供 sampler.py 读取（默认 ./sampling_schemes）")
    common.add_argument("--records-dir", default=argparse.SUPPRESS,
                        help="抽样记录目录（默认 ./sampling_records）")

    sub = parser.add_subparsers(dest="cmd", metavar="{create,list,get,update,delete,record}")

    p_create = sub.add_parser("create", parents=[common], help="创建抽样方案")
    p_create.add_argument("--name", required=True, help="方案名（建议英文+数字）")
    p_create.add_argument("--type", required=True,
                          choices=sorted(SCHEME_TYPES.keys()), help="抽样类型")
    p_create.add_argument("--params", required=True, help="参数 JSON 字符串 / 文件路径 / k=v 简写")
    p_create.add_argument("--description", default="", help="方案说明（可选）")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", parents=[common], help="列出全部方案")
    p_list.set_defaults(func=cmd_list)

    p_get = sub.add_parser("get", parents=[common], help="查看方案详情")
    p_get.add_argument("--name", required=True, help="方案名")
    p_get.set_defaults(func=cmd_get)

    p_update = sub.add_parser("update", parents=[common], help="增量更新方案参数")
    p_update.add_argument("--name", required=True, help="方案名")
    p_update.add_argument("--params", required=True, help="待更新的参数（增量合并）")
    p_update.add_argument("--type", default=None, help="同时修改抽样类型（可选）")
    p_update.add_argument("--description", default=None, help="更新方案说明（可选）")
    p_update.set_defaults(func=cmd_update)

    p_delete = sub.add_parser("delete", parents=[common], help="删除方案")
    p_delete.add_argument("--name", required=True, help="方案名")
    p_delete.set_defaults(func=cmd_delete)

    p_record = sub.add_parser("record", parents=[common], help="查看抽样记录详情")
    p_record.add_argument("--id", required=True, help="记录 ID，如 REC2024090212000012")
    p_record.set_defaults(func=cmd_record)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not getattr(args, "cmd", None):
        parser.print_help()
        print("\n[提示] 必须指定子命令：create / list / get / update / delete / record")
        sys.exit(1)

    try:
        args.func(args)
    except SystemExit:
        raise  # fail() 里已经定好退出码，直接透传
    except Exception as exc:  # 兜底，避免裸崩 traceback
        fail("执行失败: {}".format(exc), 2)


if __name__ == "__main__":
    main()
