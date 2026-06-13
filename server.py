import os
import json
import subprocess
import zipfile
import io
from datetime import datetime
from html import escape
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

DATA_FILE = "reports.json"
GITHUB_REPO_URL = os.environ.get("GITHUB_REPO_URL", "")
GITHUB_REPO_DIR = "/tmp/repo"
API_TOKEN = os.environ.get("API_TOKEN", "1panaway")

# 可选：如果你希望只允许固定 Chrome 插件来源访问 /api/report-proxy，
# 可以在 Render 环境变量里设置：ALLOWED_EXTENSION_ORIGIN=chrome-extension://你的插件ID
# 不设置则默认不限制 Origin，方便本地测试和重新加载插件。
ALLOWED_EXTENSION_ORIGIN = os.environ.get("ALLOWED_EXTENSION_ORIGIN", "")


def run_git_command(cmds, cwd=None):
    result = subprocess.run(cmds, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Git 命令执行失败: {cmds}\n{result.stderr}")
        return False
    return True


def init_git_repo():
    if not GITHUB_REPO_URL:
        print("未配置 GITHUB_REPO_URL，跳过自动同步")
        return False
    if not os.path.exists(GITHUB_REPO_DIR):
        print("克隆仓库...")
        if not run_git_command(["git", "clone", GITHUB_REPO_URL, GITHUB_REPO_DIR]):
            return False
    run_git_command(["git", "config", "user.email", "render@backup"], cwd=GITHUB_REPO_DIR)
    run_git_command(["git", "config", "user.name", "Render Backup"], cwd=GITHUB_REPO_DIR)
    return True


def sync_to_github(file_name):
    """将指定的文件同步到 GitHub 仓库"""
    if not GITHUB_REPO_URL:
        return
    if not init_git_repo():
        return
    source_path = file_name
    target_path = os.path.join(GITHUB_REPO_DIR, file_name)
    run_git_command(["cp", source_path, target_path])
    run_git_command(["git", "add", file_name], cwd=GITHUB_REPO_DIR)
    commit_msg = f"Auto-save: {datetime.now().isoformat()} - {file_name}"
    run_git_command(["git", "commit", "-m", commit_msg], cwd=GITHUB_REPO_DIR)
    run_git_command(["git", "push"], cwd=GITHUB_REPO_DIR)


def load_data():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"读取 {DATA_FILE} 失败: {e}")
        return []


def save_data(data):
    # 保存全量累积数据
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        sync_to_github(DATA_FILE)
    except Exception as e:
        print(f"同步全量文件失败: {e}")

    if data:
        latest = data[-1]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        single_file = f"reports_{timestamp}.json"
        with open(single_file, 'w', encoding='utf-8') as f:
            json.dump(latest, f, ensure_ascii=False, indent=2)
        try:
            sync_to_github(single_file)
        except Exception as e:
            print(f"同步单条记录失败: {e}")


def append_report(new_report):
    """统一保存上报记录。APK 和 Chrome 插件都会走这里，避免两套保存逻辑不一致。"""
    new_report['received_at'] = datetime.now().isoformat()
    all_data = load_data()
    all_data.append(new_report)
    save_data(all_data)
    print(
        f"收到上报：来源 {new_report.get('clientType', 'unknown')}，"
        f"用户 {new_report.get('userUid')} 提交 {new_report.get('submitted')} 条"
    )


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

    task_type = str(payload.get("taskType", "")).strip()
    # manual：本地粘贴；daily：日常任务；temp：临时任务；local：兼容可能的旧命名
    if task_type not in ["manual", "local", "daily", "temp"]:
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
            # 兼容前端万一传来 "12" 这类字符串数字
            number = int(value)
        except Exception:
            return None, (jsonify({"ok": False, "error": f"invalid_number_{field}"}), 400)

        if number < 0:
            return None, (jsonify({"ok": False, "error": f"invalid_number_{field}"}), 400)

        # 简单限幅，避免被刷入极端异常数字
        if field != "durationSeconds" and number > 100000:
            return None, (jsonify({"ok": False, "error": f"too_large_{field}"}), 400)
        if field == "durationSeconds" and number > 86400:
            return None, (jsonify({"ok": False, "error": "too_large_durationSeconds"}), 400)

        clean_report[field] = number

    # 可选字段：不影响表格展示，但方便之后排查
    for optional_field in ["version", "extensionVersion", "taskName", "note"]:
        if optional_field in payload:
            clean_report[optional_field] = str(payload.get(optional_field, ""))[:200]

    return clean_report, None


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

    # 保持 APK 原上报逻辑不变，只补一个来源标记，方便后台区分。
    new_report.setdefault('clientType', 'android-apk')
    append_report(new_report)

    return jsonify({"ok": True})


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

    # 如果你在 Render 设置了 ALLOWED_EXTENSION_ORIGIN，这里会严格检查来源。
    if ALLOWED_EXTENSION_ORIGIN:
        origin = request.headers.get("Origin", "")
        if origin != ALLOWED_EXTENSION_ORIGIN:
            return jsonify({"ok": False, "error": "origin_not_allowed"}), 403

    payload = request.get_json(silent=True)
    clean_report, error_response = validate_proxy_payload(payload)
    if error_response:
        return error_response

    append_report(clean_report)
    return jsonify({"ok": True})


@app.route('/download', methods=['GET'])
def download_data():
    if not os.path.exists(DATA_FILE):
        return "暂无数据", 404
    return send_file(DATA_FILE, as_attachment=True, download_name='reports.json')


@app.route('/download-all', methods=['GET'])
def download_all():
    """打包所有 reports_*.json 文件为 ZIP 下载"""
    files = [f for f in os.listdir('.') if f.startswith('reports_') and f.endswith('.json')]
    if not files:
        return "没有找到任何 reports_*.json 文件", 404

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file in files:
            zip_file.write(file)

    zip_buffer.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        zip_buffer,
        as_attachment=True,
        download_name=f'reports_all_{timestamp}.zip',
        mimetype='application/zip'
    )


@app.route('/')
@app.route('/view')
def index():
    all_data = load_data()
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
            table { width: 100%; border-collapse: collapse; font-size: 13px; }
            th, td { border: 1px solid #e5e5ea; padding: 8px 10px; text-align: left; }
            th { background-color: #f5f5f7; font-weight: 600; }
            tr:nth-child(even) { background-color: #fafafc; }
            .footer { margin-top: 20px; font-size: 12px; color: #8e8e93; text-align: center; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📊 豆瓣举报任务统计</h1>
            <div class="toolbar">
                <a href="/download" class="btn">⬇️ 下载 reports.json 备份</a>
                <a href="/download-all" class="btn">📦 下载全部 reports_*.json (ZIP)</a>
                <span style="font-size:13px; color:#6e6e73;">总上报次数: ''' + str(len(all_data)) + '''</span>
            </div>
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
                数据已自动同步到 GitHub 私有仓库，每次上报均生成独立备份文件。也可点击上方按钮打包下载全部 JSON 文件。
            </div>
        </div>
    </body>
    </html>
    '''
    return html


if __name__ == '__main__':
    if GITHUB_REPO_URL:
        init_git_repo()
    app.run(host='0.0.0.0', port=5000)
