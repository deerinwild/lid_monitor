import os
import json
import subprocess
import zipfile
import io
from datetime import datetime
from flask import Flask, request, jsonify, send_file, Response

app = Flask(__name__)

DATA_FILE = "reports.json"
GITHUB_REPO_URL = os.environ.get("GITHUB_REPO_URL", "")
GITHUB_REPO_DIR = "/tmp/repo"
API_TOKEN = os.environ.get("API_TOKEN", "1panaway")

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
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_data(data):
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

@app.route('/api/report', methods=['POST'])
def report():
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '')
    if token != API_TOKEN:
        return jsonify({"error": "Invalid token"}), 403

    new_report = request.get_json()
    if not new_report:
        return jsonify({"error": "No data"}), 400

    new_report['received_at'] = datetime.now().isoformat()

    all_data = load_data()
    all_data.append(new_report)
    save_data(all_data)

    print(f"收到上报：用户 {new_report.get('userUid')} 提交 {new_report.get('submitted')} 条")
    return jsonify({"ok": True})

@app.route('/download', methods=['GET'])
def download_data():
    if not os.path.exists(DATA_FILE):
        return "暂无数据", 404
    return send_file(DATA_FILE, as_attachment=True, download_name='reports.json')

@app.route('/download-all', methods=['GET'])
def download_all():
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

@app.route('/combined', methods=['GET'])
def combined():
    """展示所有 reports_*.json 文件合并后的数据表格"""
    files = [f for f in os.listdir('.') if f.startswith('reports_') and f.endswith('.json')]
    files.sort()  # 按文件名排序（时间戳顺序）
    all_records = []
    for file in files:
        with open(file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # 每个独立文件应该是单条记录（字典）
        if isinstance(data, dict):
            all_records.append(data)
        elif isinstance(data, list):
            all_records.extend(data)
    
    # 生成 HTML 表格
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>合并统计 - 所有上报记录</title>
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
            <h1>📊 合并统计 - 所有上报记录（基于独立文件）</h1>
            <div class="toolbar">
                <a href="/combined.csv" class="btn">⬇️ 下载 CSV</a>
                <a href="/" class="btn">返回累积统计</a>
                <span style="font-size:13px; color:#6e6e73;">总记录数: ''' + str(len(all_records)) + '''</span>
            </div>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>用户UID</th><th>任务类型</th><th>解析总数</th><th>提交成功</th><th>失败</th><th>白名单跳过</th><th>耗时(秒)</th><th>上报时间</th><th>服务器接收时间</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    for log in all_records:
        html += f'''
            <tr>
                <td>{log.get('userUid', '')}</td>
                <td>{log.get('taskType', '')}</td>
                <td>{log.get('totalParsed', 0)}</td>
                <td>{log.get('submitted', 0)}</td>
                <td>{log.get('failed', 0)}</td>
                <td>{log.get('whitelistSkipped', 0)}</td>
                <td>{log.get('durationSeconds', 0)}</td>
                <td>{log.get('timestamp', '')}</td>
                <td>{log.get('received_at', '')}</td>
            </tr>
        '''
    html += '''
                    </tbody>
                </table>
            </div>
            <div class="footer">
                数据来源：所有 reports_*.json 文件，按文件名字母顺序合并。
            </div>
        </div>
    </body>
    </html>
    '''
    return html

@app.route('/combined.csv', methods=['GET'])
def combined_csv():
    """生成 CSV 文件下载"""
    import csv
    from io import StringIO
    files = [f for f in os.listdir('.') if f.startswith('reports_') and f.endswith('.json')]
    files.sort()
    all_records = []
    for file in files:
        with open(file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            all_records.append(data)
        elif isinstance(data, list):
            all_records.extend(data)
    
    output = StringIO()
    fieldnames = ["userUid", "taskType", "totalParsed", "submitted", "failed", "whitelistSkipped", "durationSeconds", "timestamp", "received_at"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in all_records:
        writer.writerow({k: row.get(k, '') for k in fieldnames})
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=combined_data.csv"}
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
            <h1>📊 豆瓣举报任务统计（累积数据）</h1>
            <div class="toolbar">
                <a href="/download" class="btn">⬇️ 下载 reports.json 备份</a>
                <a href="/download-all" class="btn">📦 下载全部原始 JSON (ZIP)</a>
                <a href="/combined" class="btn">📈 查看合并统计（基于独立文件）</a>
                <span style="font-size:13px; color:#6e6e73;">总上报次数: ''' + str(len(all_data)) + '''</span>
            </div>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr><th>用户UID</th><th>任务类型</th><th>解析总数</th><th>提交成功</th><th>失败</th><th>白名单跳过</th><th>耗时(秒)</th><th>上报时间</th></tr>
                    </thead>
                    <tbody>
    '''
    for log in all_data:
        html += f'''
            <tr>
                <td>{log.get('userUid', '')}</td>
                <td>{log.get('taskType', '')}</td>
                <td>{log.get('totalParsed', 0)}</td>
                <td>{log.get('submitted', 0)}</td>
                <td>{log.get('failed', 0)}</td>
                <td>{log.get('whitelistSkipped', 0)}</td>
                <td>{log.get('durationSeconds', 0)}</td>
                <td>{log.get('timestamp', '')}</td>
            </tr>
        '''
    html += '''
                    </tbody>
                </table>
            </div>
            <div class="footer">
                数据基于 reports.json（累积所有上报）。如需查看每次上报的独立文件，请使用「合并统计」页面。
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
