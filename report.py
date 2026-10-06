"""UTF-8 批次报告；按数据库快照生成，磁盘保存使用原子替换。"""

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def timestamp(value, timezone="Asia/Tokyo"):
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S") if value else "未知"


def render_report(job, timezone="Asia/Tokyo"):
    state = "无目标条目" if not job["total"] else ("目标全部满足" if job["satisfied"] == job["total"] else "部分完成")
    lines = [f"批次：{job['name']}", f"固定 ID：{job['id']}", f"创建：{timestamp(job['created_at'], timezone)}",
             f"最近检索：{timestamp(job['last_search_at'], timezone)}", f"状态：{state}",
             f"目标：{job['total']}；已满足：{job['satisfied']}；进行中资源：{job['active']}",
             f"暂存目录：{job['temp_path']}", f"完成目录：{job['complete_path']}",
             f"报告生成：{timestamp(datetime.now().timestamp(), timezone)}", ""]
    for heading, items in (("目标清单", job["items"]), ("已移出清单，保留的任务", job["retained"])):
        lines.extend([heading, "=" * 60])
        for item in items:
            lines.extend([f"{item['ordinal']:04d} {item['query']}", f"原始输入：{item['original_text']}",
                          f"条目：{'已满足' if item['satisfied'] else '未满足'}；查询：{item['state']}"])
            if item["error"]:
                lines.append("检索错误：" + item["error"])
            if not item["downloads"]:
                lines.append("尚未添加下载")
            for link in item["downloads"]:
                task = link["task"]
                completed = task["completion_on"] or task["completed_observed_at"]
                completion_label = "完成时间" if task["completion_on"] else "首次发现完成"
                lines.extend([f"  {'[采用]' if link['adopted'] else '[备选]'} {task['title']}",
                              f"  来源：{link['source']}；添加时关键词：{link['query']}",
                              f"  哈希：{task['identity']}",
                              f"  提交：{timestamp(task['submitted_at'], timezone)}；下载器加入：{timestamp(task['added_on'], timezone)}",
                              f"  状态：{task['state']}；提交状态：{task['submission_state']}；进度：{task['progress']:.1%}",
                              f"  {completion_label}：{timestamp(completed, timezone)}；最后同步：{timestamp(task['last_sync'], timezone)}",
                              f"  文件：{link['file_state']}；交付位置：{link['target_path']}",
                              f"  原下载位置：{task['content_path'] or task['save_path'] or task['requested_save_path']}",
                              f"  暂存位置：{task['requested_temp_path']}"])
                errors = [link["file_error"], task["error"], task["sync_error"]]
                if any(errors):
                    lines.append("  提示：" + "；".join(value for value in errors if value))
            lines.append("")
    return "\n".join(lines) + "\n"


def save_report(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("report.txt.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
