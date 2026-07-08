"""chaoxing_auto 包初始化。

将项目根目录加入 sys.path，保证既能按脚本方式运行，也能按包方式导入。
"""

from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
