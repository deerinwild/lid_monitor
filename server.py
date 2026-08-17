import os
import json
import subprocess
import zipfile
import io
import glob
import shutil
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

DATA_FILE = "reports.json"
GITHUB_REPO_URL = os.environ.get("GITHUB_REPO_URL", "")
GITHUB_REPO_DIR = os.environ.get("GITHUB_REPO_DIR", "/tmp/repo")
API_TOKEN = os.environ.get("API_TOKEN", "1panaway")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", API_TOKEN)
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GIT_COMMAND_TIMEOUT = int(os.environ.get("GIT_COMMAND_TIMEOUT", "12"))

# 可选：如果你希望只允许固定 Chrome 插件来源访问 /api/report-proxy，
# 可以在 Render 环境变量里设置：ALLOWED_EXTENSION_ORIGIN=chrome-extension://你的插件ID
# 不设置则默认不限制 Origin，方便本地测试和重新加载插件。
ALLOWED_EXTENSION_ORIGIN = os.environ.get("ALLOWED_EXTENSION_ORIGIN", "")

SERVER_TIMEZONE = "Asia/Shanghai"
TZ = ZoneInfo(SERVER_TIMEZONE)
ARCHIVE_RECORDS_DIR = "archive/reports"
ARCHIVE_SUMMARY_DIR = "archive/summary"
LATEST_INDEX_PATH = os.environ.get("LATEST_INDEX_PATH", "latest.json")
LATEST_INDEX_MAX_DAYS = int(os.environ.get("LATEST_INDEX_MAX_DAYS", "90"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_datetime(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        # 旧版 received_at 没有时区；Render 默认通常是 UTC，这里按 UTC 兼容。
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def beijing_day_key(date_like=None):
    if isinstance(date_like, str):
        dt = parse_datetime(date_like)
    else:
        dt = date_like
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%Y-%m-%d")


def is_day_key(value):
    text = str(value or "")
    if len(text) != 10:
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except Exception:
        return False


def normalize_date(raw=None):
    text = str(raw or "").strip()
    return text if is_day_key(text) else beijing_day_key()


def record_day_key(record):
    # 归档日期统一按北京时间。优先使用服务器接收时间，其次使用插件上报时间。
    for field in ("received_at", "timestamp"):
        dt = parse_datetime(record.get(field))
        if dt:
            return beijing_day_key(dt)
    return beijing_day_key()


def month_info(date):
    year, month, _ = date.split("-")
    return year, month, f"{year}-{month}"


def archive_paths_for_date(date):
    year, month, month_key = month_info(date)
    records_path = f"{ARCHIVE_RECORDS_DIR}/{year}/{month}/{date}.json"
    summary_path = f"{ARCHIVE_SUMMARY_DIR}/{year}/{month_key}.json"
    return records_path, summary_path, month_key


def count(value, fallback=0):
    try:
        n = int(value)
        return n if n >= 0 else fallback
    except Exception:
        return fallback


def normalize_task_type(value):
    raw = str(value or "").strip()
    if raw == "manual":
        return "local"
    return raw or "unknown"


def run_git_command(cmds, cwd=None, quiet=False, timeout=None):
    timeout = GIT_COMMAND_TIMEOUT if timeout is None else timeout
    try:
        result = subprocess.run(
            cmds, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        if not quiet:
            print(f"Git 命令超时（>{timeout}s）: {cmds}")
        return False
    except Exception as e:
        if not quiet:
            print(f"Git 命令异常: {cmds}\n{e}")
        return False

    if result.returncode != 0:
        if not quiet:
            print(f"Git 命令执行失败: {cmds}\n{result.stderr}")
        return False
    return True


def init_git_repo(refresh=True):
    """确保本地仓库处于目标分支，并在任何 checkout/commit 前配置 Git 身份。"""
    if not GITHUB_REPO_URL:
        print("未配置 GITHUB_REPO_URL，跳过自动同步")
        return False

    git_dir = os.path.join(GITHUB_REPO_DIR, ".git")
    if not os.path.exists(git_dir):
        if os.path.exists(GITHUB_REPO_DIR):
            shutil.rmtree(GITHUB_REPO_DIR, ignore_errors=True)
        print(f"克隆仓库分支 {GITHUB_BRANCH}...")
        if not run_git_command(
            [
                "git", "clone", "--branch", GITHUB_BRANCH, "--single-branch",
                GITHUB_REPO_URL, GITHUB_REPO_DIR
            ],
            timeout=max(GIT_COMMAND_TIMEOUT, 20),
        ):
            print(
                f"无法克隆远端分支 {GITHUB_BRANCH}。请检查 Render 的 "
                "GITHUB_BRANCH 是否与 GitHub 仓库实际分支一致。"
            )
            return False

    # 无论后续 fetch / checkout 是否成功，先写入仓库级 Git 身份。
    if not run_git_command(
        ["git", "config", "user.email", "render@backup"],
        cwd=GITHUB_REPO_DIR,
    ):
        return False
    if not run_git_command(
        ["git", "config", "user.name", "Render Backup"],
        cwd=GITHUB_REPO_DIR,
    ):
        return False

    if refresh:
        if not run_git_command(
            ["git", "fetch", "origin", GITHUB_BRANCH],
            cwd=GITHUB_REPO_DIR,
        ):
            return False

        # 上一次失败可能在 /tmp/repo 留下未提交修改。
        # 这些文件只是 Render 待同步副本；真实待写数据仍保存在项目工作目录，
        # 因此这里先清理仓库副本，再对齐远端分支，避免 checkout 被阻塞。
        run_git_command(
            ["git", "reset", "--hard", f"origin/{GITHUB_BRANCH}"],
            cwd=GITHUB_REPO_DIR,
            quiet=True,
        )
        run_git_command(
            ["git", "clean", "-fd"],
            cwd=GITHUB_REPO_DIR,
            quiet=True,
        )

        if not run_git_command(
            ["git", "checkout", "-B", GITHUB_BRANCH, f"origin/{GITHUB_BRANCH}"],
            cwd=GITHUB_REPO_DIR,
        ):
            return False

    return True


def read_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"读取 JSON 失败 {path}: {e}")
        return default


def write_json_file(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_repo_or_local_json(rel_path, default):
    repo_path = os.path.join(GITHUB_REPO_DIR, rel_path)
    if GITHUB_REPO_URL and os.path.exists(repo_path):
        return read_json_file(repo_path, default)
    return read_json_file(rel_path, default)


def commit_repo_paths(rel_paths, message, repo_ready=False):
    if not GITHUB_REPO_URL:
        return False
    # append_record_to_archive() 已经刷新过仓库时，不要再次 fetch。
    if not repo_ready and not init_git_repo(refresh=True):
        return False

    clean_paths = []
    for rel_path in dict.fromkeys(rel_paths):
        if not rel_path:
            continue
        clean_paths.append(rel_path)
        src = rel_path
        dst = os.path.join(GITHUB_REPO_DIR, rel_path)
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst) or GITHUB_REPO_DIR, exist_ok=True)
            shutil.copy2(src, dst)

    if not clean_paths:
        return False
    run_git_command(["git", "add", *clean_paths], cwd=GITHUB_REPO_DIR)
    # 没有变化时不提交，避免 Render 日志报错。
    if run_git_command(["git", "diff", "--cached", "--quiet"], cwd=GITHUB_REPO_DIR, quiet=True):
        return True
    if not run_git_command(["git", "commit", "-m", message], cwd=GITHUB_REPO_DIR):
        return False
    return run_git_command(["git", "push", "origin", GITHUB_BRANCH], cwd=GITHUB_REPO_DIR)


def load_data():
    data = read_json_file(DATA_FILE, [])
    return data if isinstance(data, list) else []


def save_session_data(data):
    # 仅保存 Render 当前进程内的短期备份。展示和 GitHub Pages 以后不再依赖根目录 reports_*.json。
    write_json_file(DATA_FILE, data if isinstance(data, list) else [])


def last_seen_iso(records):
    latest_dt = None
    latest_text = ""
    for r in records:
        for field in ("received_at", "timestamp"):
            dt = parse_datetime(r.get(field))
            if dt and (latest_dt is None or dt > latest_dt):
                latest_dt = dt
                latest_text = r.get(field) or dt.isoformat()
    return latest_dt.isoformat().replace("+00:00", "Z") if latest_dt else latest_text


def summarize_records(date, records, records_path):
    type_counts = {"daily": 0, "temp": 0, "local": 0, "unknown": 0}
    user_set = set()
    for r in records:
        uid = str(r.get("userUid", "")).strip()
        if uid:
            user_set.add(uid)
        t = normalize_task_type(r.get("taskType"))
        type_counts[t] = type_counts.get(t, 0) + 1
    return {
        "date": date,
        "recordsPath": records_path,
        "reportCount": len(records),
        "activeUsers": len(user_set),
        "totalParsed": sum(count(r.get("totalParsed")) for r in records),
        "submitted": sum(count(r.get("submitted")) for r in records),
        "failed": sum(count(r.get("failed")) for r in records),
        "whitelistSkipped": sum(count(r.get("whitelistSkipped")) for r in records),
        "durationSeconds": sum(count(r.get("durationSeconds")) for r in records),
        "taskTypes": type_counts,
        "lastSeenAt": last_seen_iso(records),
        "updatedAt": now_iso(),
    }


def default_latest_index():
    return {
        "version": 1,
        "updatedAt": "",
        "latestDate": "",
        "latestMonth": "",
        "latestRecordsPath": "",
        "latestSummaryPath": "",
        "days": [],
    }


def normalize_latest_entry(summary, summary_path, month_key):
    return {
        "date": summary.get("date", ""),
        "month": month_key,
        "recordsPath": summary.get("recordsPath", ""),
        "summaryPath": summary_path,
        "updatedAt": summary.get("updatedAt", now_iso()),
        "reportCount": count(summary.get("reportCount")),
        "activeUsers": count(summary.get("activeUsers")),
        "totalParsed": count(summary.get("totalParsed")),
        "submitted": count(summary.get("submitted")),
        "failed": count(summary.get("failed")),
        "whitelistSkipped": count(summary.get("whitelistSkipped")),
        "lastSeenAt": summary.get("lastSeenAt", ""),
    }


def update_latest_index(latest, entry):
    latest = latest if isinstance(latest, dict) else default_latest_index()
    merged = {}
    for item in latest.get("days", []) if isinstance(latest.get("days"), list) else []:
        if item and is_day_key(item.get("date")):
            merged[item["date"]] = item
    if entry and is_day_key(entry.get("date")):
        merged[entry["date"]] = entry
    days = sorted(merged.values(), key=lambda item: item.get("date", ""), reverse=True)[:max(1, LATEST_INDEX_MAX_DAYS)]
    latest.update(default_latest_index())
    latest["version"] = 1
    latest["updatedAt"] = now_iso()
    latest["days"] = days
    if days:
        first = days[0]
        latest["latestDate"] = first.get("date", "")
        latest["latestMonth"] = first.get("month", "")
        latest["latestRecordsPath"] = first.get("recordsPath", "")
        latest["latestSummaryPath"] = first.get("summaryPath", "")
    return latest


def append_record_to_archive(record):
    # 先拉取远端，再基于远端当天文件追加，避免 Render 重启后覆盖 GitHub 中已有日期数据。
    repo_ready = init_git_repo(refresh=True)

    date = record_day_key(record)
    records_path, summary_path, month_key = archive_paths_for_date(date)

    records = read_repo_or_local_json(records_path, [])
    if not isinstance(records, list):
        records = []
    records.append(record)
    write_json_file(records_path, records)

    summary = read_repo_or_local_json(summary_path, {"month": month_key, "days": {}})
    if not isinstance(summary, dict):
        summary = {"month": month_key, "days": {}}
    summary["month"] = month_key
    summary["days"] = summary.get("days") if isinstance(summary.get("days"), dict) else {}
    day_summary = summarize_records(date, records, records_path)
    summary["days"][date] = day_summary
    summary["updatedAt"] = now_iso()
    write_json_file(summary_path, summary)

    latest = read_repo_or_local_json(LATEST_INDEX_PATH, default_latest_index())
    latest = update_latest_index(latest, normalize_latest_entry(day_summary, summary_path, month_key))
    write_json_file(LATEST_INDEX_PATH, latest)

    if repo_ready:
        commit_repo_paths(
            [records_path, summary_path, LATEST_INDEX_PATH, DATA_FILE],
            f"Auto-save task report {date}",
            repo_ready=True,
        )
    else:
        print("Git 仓库未就绪：本次数据仅写入 Render 本地，未尝试 commit/push")
    return date, records_path


def append_report(new_report):
    """统一保存上报记录。APK 和 Chrome 插件都会走这里，避免两套保存逻辑不一致。"""
    new_report["received_at"] = now_iso()
    new_report["taskType"] = normalize_task_type(new_report.get("taskType"))

    all_data = load_data()
    all_data.append(new_report)
    save_session_data(all_data)

    archive_date, archive_path = append_record_to_archive(new_report)
    print(
        f"收到上报：来源 {new_report.get('clientType', 'unknown')}，"
        f"用户 {new_report.get('userUid')} 提交 {new_report.get('submitted')} 条，"
        f"已写入 {archive_path}"
    )
    return archive_date, archive_path


def validate_proxy_payload(payload):
    """校验 Chrome 插件中转上报数据，防止空数据和明显异常数据写入 reports.json。"""
    if not isinstance(payload, dict):
        return None, (jsonify({"ok": False, "error": "empty_payload"}), 400)

    required_fields = [
        "userUid",
        "taskType",
        "totalParsed",
        "submitted",
        "failed",
        "whitelistSkipped",
        "durationSeconds",
        "timestamp"
    ]

    for field in required_fields:
        if field not in payload:
            return None, (jsonify({"ok": False, "error": f"missing_field_{field}"}), 400)

    user_uid = str(payload.get("userUid", "")).strip()
    if not user_uid.isdigit():
        return None, (jsonify({"ok": False, "error": "invalid_userUid"}), 400)

    task_type = normalize_task_type(payload.get("taskType"))
    if task_type not in ["local", "daily", "temp"]:
        return None, (jsonify({"ok": False, "error": "invalid_taskType"}), 400)

    number_fields = [
        "totalParsed",
        "submitted",
        "failed",
        "whitelistSkipped",
        "durationSeconds"
    ]

    clean_report = {
        "userUid": user_uid,
        "taskType": task_type,
        "timestamp": str(payload.get("timestamp", "")).strip(),
        "clientType": "chrome-extension"
    }

    for field in number_fields:
        value = payload.get(field)
        try:
            number = int(value)
        except Exception:
            return None, (jsonify({"ok": False, "error": f"invalid_number_{field}"}), 400)

        if number < 0:
            return None, (jsonify({"ok": False, "error": f"invalid_number_{field}"}), 400)

        if field != "durationSeconds" and number > 100000:
            return None, (jsonify({"ok": False, "error": f"too_large_{field}"}), 400)
        if field == "durationSeconds" and number > 86400:
            return None, (jsonify({"ok": False, "error": "too_large_durationSeconds"}), 400)

        clean_report[field] = number

    for optional_field in ["version", "extensionVersion", "taskName", "note"]:
        if optional_field in payload:
            clean_report[optional_field] = str(payload.get(optional_field, ""))[:200]

    return clean_report, None


def require_admin_request():
    token = str(request.args.get("token") or request.headers.get("X-Admin-Token") or "")
    return bool(ADMIN_TOKEN and token == ADMIN_TOKEN)


def record_identity(record):
    # 用于迁移脚本去重；正常上报仍保留每次上报。
    keys = [
        "userUid", "taskType", "timestamp", "received_at", "totalParsed",
        "submitted", "failed", "whitelistSkipped", "durationSeconds", "clientType"
    ]
    return json.dumps({k: record.get(k) for k in keys}, ensure_ascii=False, sort_keys=True)


def migrate_legacy_reports(limit=0):
    if not init_git_repo():
        return {"ok": False, "error": "github_not_configured_or_clone_failed"}

    legacy_files = sorted(glob.glob(os.path.join(GITHUB_REPO_DIR, "reports_*.json")))
    if limit and limit > 0:
        legacy_files = legacy_files[:limit]

    collected = []
    for path in legacy_files:
        data = read_json_file(path, None)
        if isinstance(data, list):
            collected.extend([x for x in data if isinstance(x, dict)])
        elif isinstance(data, dict):
            collected.append(data)

    root_reports = read_json_file(os.path.join(GITHUB_REPO_DIR, DATA_FILE), [])
    if isinstance(root_reports, list):
        collected.extend([x for x in root_reports if isinstance(x, dict)])

    grouped = {}
    for r in collected:
        r = dict(r)
        r["taskType"] = normalize_task_type(r.get("taskType"))
        if not r.get("received_at"):
            r["received_at"] = r.get("timestamp") or now_iso()
        date = record_day_key(r)
        grouped.setdefault(date, []).append(r)

    changed_paths = []
    changed_days = []
    added = 0
    for date, new_records in grouped.items():
        records_path, summary_path, month_key = archive_paths_for_date(date)
        repo_records_path = os.path.join(GITHUB_REPO_DIR, records_path)
        existing = read_json_file(repo_records_path, [])
        if not isinstance(existing, list):
            existing = []
        seen = {record_identity(x) for x in existing if isinstance(x, dict)}
        before = len(existing)
        for r in new_records:
            key = record_identity(r)
            if key not in seen:
                existing.append(r)
                seen.add(key)
        if len(existing) == before:
            continue

        write_json_file(repo_records_path, existing)
        day_summary = summarize_records(date, existing, records_path)

        repo_summary_path = os.path.join(GITHUB_REPO_DIR, summary_path)
        summary = read_json_file(repo_summary_path, {"month": month_key, "days": {}})
        if not isinstance(summary, dict):
            summary = {"month": month_key, "days": {}}
        summary["month"] = month_key
        summary["days"] = summary.get("days") if isinstance(summary.get("days"), dict) else {}
        summary["days"][date] = day_summary
        summary["updatedAt"] = now_iso()
        write_json_file(repo_summary_path, summary)

        changed_paths.extend([records_path, summary_path])
        changed_days.append(normalize_latest_entry(day_summary, summary_path, month_key))
        added += len(existing) - before

    if changed_days:
        repo_latest_path = os.path.join(GITHUB_REPO_DIR, LATEST_INDEX_PATH)
        latest = read_json_file(repo_latest_path, default_latest_index())
        for entry in changed_days:
            latest = update_latest_index(latest, entry)
        write_json_file(repo_latest_path, latest)
        changed_paths.append(LATEST_INDEX_PATH)

    if changed_paths:
        run_git_command(["git", "add", *sorted(set(changed_paths))], cwd=GITHUB_REPO_DIR)
        if not run_git_command(["git", "diff", "--cached", "--quiet"], cwd=GITHUB_REPO_DIR, quiet=True):
            run_git_command(["git", "commit", "-m", f"Migrate legacy task reports into archive ({added} records)"], cwd=GITHUB_REPO_DIR)
            run_git_command(["git", "push", "origin", GITHUB_BRANCH], cwd=GITHUB_REPO_DIR)

    return {
        "ok": True,
        "legacyFilesScanned": len(legacy_files),
        "recordsScanned": len(collected),
        "recordsAddedToArchive": added,
        "daysChanged": sorted({x.get("date") for x in changed_days}, reverse=True),
        "changedPaths": sorted(set(changed_paths)),
    }


@app.after_request
def add_cors_headers(response):
    """给 Chrome 插件中转接口补 CORS；不改变 APK 使用的 /api/report 逻辑。"""
    if request.path == "/api/report-proxy":
        origin = request.headers.get("Origin", "")
        if ALLOWED_EXTENSION_ORIGIN:
            if origin == ALLOWED_EXTENSION_ORIGIN:
                response.headers["Access-Control-Allow-Origin"] = origin
        else:
            response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route('/api/report', methods=['POST'])
def report():
    """手机端 APK 继续使用这个接口：必须携带 Authorization: Bearer 1panaway。"""
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '')
    if token != API_TOKEN:
        return jsonify({"error": "Invalid token"}), 403

    new_report = request.get_json(silent=True)
    if not new_report:
        return jsonify({"error": "No data"}), 400

    if not isinstance(new_report, dict):
        return jsonify({"error": "Invalid data"}), 400

    new_report.setdefault('clientType', 'android-apk')
    archive_date, archive_path = append_report(new_report)

    return jsonify({"ok": True, "archiveDate": archive_date, "archivePath": archive_path})


@app.route('/api/report-proxy', methods=['POST', 'OPTIONS'])
def report_proxy():
    """
    Chrome 插件使用这个接口：
    1. 插件端不需要也不应该携带 API_TOKEN；
    2. 后端只接收格式正确的统计数据；
    3. 保存逻辑与 /api/report 共用 append_report，不影响手机端 APK。
    """
    if request.method == 'OPTIONS':
        return ('', 204)

    if ALLOWED_EXTENSION_ORIGIN:
        origin = request.headers.get("Origin", "")
        if origin != ALLOWED_EXTENSION_ORIGIN:
            return jsonify({"ok": False, "error": "origin_not_allowed"}), 403

    payload = request.get_json(silent=True)
    clean_report, error_response = validate_proxy_payload(payload)
    if error_response:
        return error_response

    archive_date, archive_path = append_report(clean_report)
    return jsonify({"ok": True, "archiveDate": archive_date, "archivePath": archive_path})


@app.route('/health', methods=['GET'])
def health():
    latest = read_repo_or_local_json(LATEST_INDEX_PATH, default_latest_index())
    return jsonify({
        "ok": True,
        "service": "lid_monitor",
        "timezone": SERVER_TIMEZONE,
        "serverBeijingDate": beijing_day_key(),
        "githubEnabled": bool(GITHUB_REPO_URL),
        "githubRepoDir": GITHUB_REPO_DIR,
        "branch": GITHUB_BRANCH,
        "latestIndexPath": LATEST_INDEX_PATH,
        "latestDate": latest.get("latestDate", "") if isinstance(latest, dict) else "",
    })


@app.route('/api/migrate-legacy', methods=['GET', 'POST'])
def migrate_legacy():
    if not require_admin_request():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    limit = count(request.args.get("limit"), 0)
    result = migrate_legacy_reports(limit=limit)
    return jsonify(result)


@app.route('/download', methods=['GET'])
def download_data():
    if not os.path.exists(DATA_FILE):
        return "暂无数据", 404
    return send_file(DATA_FILE, as_attachment=True, download_name='reports.json')


@app.route('/download-all', methods=['GET'])
def download_all():
    """打包当前 Render 本地 reports_*.json 和 archive/reports/**/*.json 为 ZIP 下载。"""
    files = [f for f in glob.glob('reports_*.json') if os.path.isfile(f)]
    files += [f for f in glob.glob(f'{ARCHIVE_RECORDS_DIR}/**/*.json', recursive=True) if os.path.isfile(f)]
    if not files:
        return "没有找到任何 reports_*.json 或 archive/reports/**/*.json 文件", 404

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file in files:
            zip_file.write(file)

    zip_buffer.seek(0)
    timestamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    return send_file(
        zip_buffer,
        as_attachment=True,
        download_name=f'reports_all_{timestamp}.zip',
        mimetype='application/zip'
    )


def load_records_from_archive(date=None):
    latest = read_repo_or_local_json(LATEST_INDEX_PATH, default_latest_index())
    target_date = normalize_date(date) if date else ""
    records_path = ""
    source_label = "local-session"

    if target_date:
        records_path, _, _ = archive_paths_for_date(target_date)
    elif isinstance(latest, dict) and latest.get("latestRecordsPath"):
        target_date = latest.get("latestDate", "")
        records_path = latest.get("latestRecordsPath", "")

    if records_path:
        records = read_repo_or_local_json(records_path, [])
        if isinstance(records, list):
            source_label = records_path
            return target_date, source_label, records
    return target_date or beijing_day_key(), source_label, load_data()


@app.route('/')
@app.route('/view')
def index():
    date_arg = request.args.get('date')
    current_date, source_label, all_data = load_records_from_archive(date_arg)
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>豆瓣举报统计后台</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f5f5f7; margin: 0; padding: 20px; }
            .container { max-width: 1200px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); padding: 20px; }
            h1 { font-size: 24px; margin-top: 0; color: #1d1d1f; }
            .toolbar { margin-bottom: 20px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
            .btn { background-color: #1d1d1f; color: white; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 14px; text-decoration: none; display: inline-block; }
            .btn:hover { background-color: #3a3a3c; }
            input { border: 1px solid #d1d1d6; border-radius: 8px; padding: 7px 10px; font-size: 14px; }
            table { width: 100%; border-collapse: collapse; font-size: 13px; }
            th, td { border: 1px solid #e5e5ea; padding: 8px 10px; text-align: left; }
            th { background-color: #f5f5f7; font-weight: 600; }
            tr:nth-child(even) { background-color: #fafafc; }
            .footer { margin-top: 20px; font-size: 12px; color: #8e8e93; text-align: center; }
            .muted { font-size:13px; color:#6e6e73; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📊 豆瓣举报任务统计</h1>
            <form class="toolbar" method="get" action="/view">
                <a href="/download" class="btn">⬇️ 下载当前进程 reports.json</a>
                <a href="/download-all" class="btn">📦 下载本地归档 ZIP</a>
                <span class="muted">显示日期: ''' + escape(str(current_date)) + '''</span>
                <span class="muted">数据源: ''' + escape(str(source_label)) + '''</span>
                <input type="date" name="date" value="''' + escape(str(current_date)) + '''">
                <button class="btn" type="submit">加载日期</button>
                <span class="muted">上报次数: ''' + str(len(all_data)) + '''</span>
            </form>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr><th>用户UID</th><th>客户端</th><th>任务类型</th><th>解析总数</th><th>提交成功</th><th>失败</th><th>白名单跳过</th><th>耗时(秒)</th><th>上报时间</th><th>接收时间</th></tr>
                    </thead>
                    <tbody>
    '''
    for log in all_data:
        html += f'''
            <tr>
                <td>{escape(str(log.get('userUid', '')))}</td>
                <td>{escape(str(log.get('clientType', '')))}</td>
                <td>{escape(str(log.get('taskType', '')))}</td>
                <td>{escape(str(log.get('totalParsed', 0)))}</td>
                <td>{escape(str(log.get('submitted', 0)))}</td>
                <td>{escape(str(log.get('failed', 0)))}</td>
                <td>{escape(str(log.get('whitelistSkipped', 0)))}</td>
                <td>{escape(str(log.get('durationSeconds', 0)))}</td>
                <td>{escape(str(log.get('timestamp', '')))}</td>
                <td>{escape(str(log.get('received_at', '')))}</td>
            </tr>
        '''
    html += '''
                    </tbody>
                </table>
            </div>
            <div class="footer">
                新数据已按 <code>archive/reports/年/月/日期.json</code> 写入 GitHub，并通过 <code>latest.json</code> 提供最新索引；不再依赖仓库根目录 reports_*.json 列表。
            </div>
        </div>
    </body>
    </html>
    '''
    return html


if __name__ == '__main__':
    if GITHUB_REPO_URL:
        init_git_repo()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')))
