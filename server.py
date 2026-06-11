import os
import json
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# 数据文件路径
DATA_FILE = "reports.json"

# GitHub 仓库配置（从环境变量读取，避免硬编码）
GITHUB_REPO_URL = os.environ.get("GITHUB_REPO_URL", "")
GITHUB_REPO_DIR = "/tmp/repo"  # Render 临时目录，可写

# 安全令牌（必须与 App 中一致）
API_TOKEN = os.environ.get("API_TOKEN", "1panaway")

def run_git_command(cmds, cwd=None):
    """执行 git 命令，返回是否成功"""
    result = subprocess.run(cmds, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Git 命令执行失败: {cmds}\n{result.stderr}")
        return False
    return True

def init_git_repo():
    """初始化 Git 仓库（首次运行时克隆）"""
    if not GITHUB_REPO_URL:
        print("未配置 GITHUB_REPO_URL，跳过自动同步")
        return False
    if not os.path.exists(GITHUB_REPO_DIR):
        print("克隆仓库...")
        if not run_git_command(["git", "clone", GITHUB_REPO_URL, GITHUB_REPO_DIR]):
            return False
    # 设置用户信息（用于 commit）
    run_git_command(["git", "config", "user.email", "render@backup"], cwd=GITHUB_REPO_DIR)
    run_git_command(["git", "config", "user.name", "Render Backup"], cwd=GITHUB_REPO_DIR)
    return True

def sync_to_github():
    """将当前 DATA_FILE 同步到 GitHub"""
    if not GITHUB_REPO_URL:
        return
    if not init_git_repo():
        return
    
    target_file = os.path.join(GITHUB_REPO_DIR, DATA_FILE)
    # 复制文件到仓库目录
    run_git_command(["cp", DATA_FILE, target_file])
    # 添加变更
    if not run_git_command(["git", "add", DATA_FILE], cwd=GITHUB_REPO_DIR):
        return
    # 提交
    commit_msg = f"Auto-save: {datetime.now().isoformat()}"
    if not run_git_command(["git", "commit", "-m", commit_msg], cwd=GITHUB_REPO_DIR):
        # 如果没有变更，commit 会失败，这是正常的
        return
    # 推送
    run_git_command(["git", "push"], cwd=GITHUB_REPO_DIR)

def load_data():
    """加载已有数据"""
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_data(data):
    """保存数据到文件并同步到 GitHub"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # 异步同步到 GitHub（可改为后台线程，但简单场景够用）
    try:
        sync_to_github()
    except Exception as e:
        print(f"同步到 GitHub 失败: {e}")

@app.route('/api/report', methods=['POST'])
def report():
    """接收 App 上报的数据"""
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
    """下载 reports.json 文件"""
    if not os.path.exists(DATA_FILE):
        return "暂无数据", 404
    return send_file(DATA_FILE, as_attachment=True, download_name='reports.json')

@app.route('/')
@app.route('/view')
def index():
    """展示数据表格的网页（可通过 / 或 /view 访问）"""
    all_data = load_data()
    
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>豆瓣举报统计后台</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                background-color: #f5f5f7;
                margin: 0;
                padding: 20px;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
                background: white;
                border-radius: 16px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.05);
                padding: 20px;
            }
            h1 {
                font-size: 24px;
                margin-top: 0;
                color: #1d1d1f;
            }
            .toolbar {
                margin-bottom: 20px;
                display: flex;
                gap: 12px;
                align-items: center;
                flex-wrap: wrap;
            }
            .btn {
                background-color: #1d1d1f;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 8px;
                cursor: pointer;
                font-size: 14px;
                text-decoration: none;
                display: inline-block;
            }
            .btn:hover {
                background-color: #3a3a3c;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                font-size: 13px;
            }
            th, td {
                border: 1px solid #e5e5ea;
                padding: 8px 10px;
                text-align: left;
            }
            th {
                background-color: #f5f5f7;
                font-weight: 600;
            }
            tr:nth-child(even) {
                background-color: #fafafc;
            }
            .footer {
                margin-top: 20px;
                font-size: 12px;
                color: #8e8e93;
                text-align: center;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📊 豆瓣举报任务统计</h1>
            <div class="toolbar">
                <a href="/download" class="btn">⬇️ 下载 reports.json 备份</a>
                <span style="font-size:13px; color:#6e6e73;">总上报次数: ''' + str(len(all_data)) + '''</span>
            </div>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>用户UID</th><th>任务类型</th><th>解析总数</th><th>提交成功</th><th>失败</th><th>白名单跳过</th><th>耗时(秒)</th><th>上报时间</th>
                        </tr>
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
                数据已自动同步到 GitHub 私有仓库，建议定期检查。
            </div>
        </div>
    </body>
    </html>
    '''
    return html

if __name__ == '__main__':
    # 启动前尝试初始化 Git 仓库（可选）
    if GITHUB_REPO_URL:
        init_git_repo()
    app.run(host='0.0.0.0', port=5000)
