"""Honest startup shell; no business services or user-data IO are connected."""
import sys

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget


def create_main_window() -> QMainWindow:
    window = QMainWindow()
    window.setWindowTitle("Personal Markdown Knowledge Base · Python 开发基线")
    window.resize(820, 400)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(28, 28, 28, 28)
    texts = (
        "个人 Markdown 笔记与知识管理工具",
        "Python + PySide6 + SQLite · 团队启动基线",
        "A：集合与集成  /  B：界面编辑  /  C：存储检索引用  /  D：导出备份恢复",
        "业务服务尚未接入：当前窗口不读写笔记、不创建集合。",
        "基础检查与启动检查不代表完整产品验收；V01—V16仍未执行。",
    )
    for index, text in enumerate(texts):
        label = QLabel(text)
        label.setWordWrap(True)
        if index == 0:
            font = label.font()
            font.setPointSize(20)
            label.setFont(font)
        layout.addWidget(label)
    window.setCentralWidget(body)
    return window


def main() -> int:
    app = QApplication(sys.argv)
    window = create_main_window()
    window.show()
    return app.exec()
