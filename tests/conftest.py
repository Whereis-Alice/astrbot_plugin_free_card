import sys
from pathlib import Path
from types import ModuleType

package = ModuleType("free_card_plugin")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
