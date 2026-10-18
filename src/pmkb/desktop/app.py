"""Development shell with runtime lifecycle, no editor or user-data IO."""
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget
from pmkb.application.runtime import AsyncServiceRuntime
from pmkb.desktop.dispatcher import QtDispatcher


class StartupWindow(QMainWindow):
    """No drafts in this shell. B must add business exit guards before reuse."""
    def __init__(self):
        super().__init__()
        self.runtime = AsyncServiceRuntime()
        self.dispatcher = QtDispatcher()
        self._closing = False
        self._closed_runtime = False
        self._poll = QTimer(self)
        self._poll.setInterval(10)
        self._poll.timeout.connect(self._check_shutdown)

    def closeEvent(self, event):
        if self._closed_runtime:
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self.centralWidget().setEnabled(False)
            self.statusBar().showMessage('正在等待后台任务结束；尚未确认的结果不会被视为已撤销。')
            self.runtime.begin_shutdown()
            self._poll.start()

    def _check_shutdown(self):
        if not self.runtime.is_stopped or self.dispatcher.pending_count:
            return
        self._poll.stop()
        self._closed_runtime = True
        self.dispatcher.close()
        if self.runtime.shutdown_errors:
            # Keep the error visible; the next explicit close acknowledges it.
            self.statusBar().showMessage('后台已停止，但资源清理发生错误；请核查后再次关闭窗口。')
            return
        self.close()


def create_main_window() -> StartupWindow:
    window = StartupWindow()
    window.setWindowTitle("Personal Markdown Knowledge Base · Python 开发基线")
    window.resize(820, 400)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(28, 28, 28, 28)
    texts = (
        "个人 Markdown 笔记与知识管理工具",
        "Python + PySide6 + SQLite · 团队启动基线",
        "A：集合与集成  /  B：界面编辑  /  C：存储检索引用  /  D：导出备份恢复",
        "后台运行与Qt回调桥已具备；当前窗口不读写笔记、不创建集合。",
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
