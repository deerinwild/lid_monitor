from flask import Flask, request, jsonify, send_file
import json
from datetime import datetime
import os

app = Flask(__name__)

# 数据文件路径（Render 允许写入当前目录）
DATA_FILE = "reports.json"
# 简单令牌验证（与 App 中保持一致）
API_TOKEN = "your-secret-token"   # 建议修改成一个复杂字符串

def load_data():
    """加载已有数据"""
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_data(data):
    """保存数据到文件"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
                数据仅存储于服务器本地文件，建议定期下载备份。
            </div>
        </div>
    </body>
    </html>
    '''
    return html

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)