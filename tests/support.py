"""测试仅写入临时目录，不使用真实下载根目录。"""
from copy import deepcopy
from pathlib import Path
from main import config


def settings_for(directory):
    settings = deepcopy(config)
    settings["download"].update(temp_root=str(Path(directory) / "temp"), complete_root=str(Path(directory) / "complete"))
    return settings
