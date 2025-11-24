import json
import locale
import os
import sys
from typing import Dict, Optional

_translations: Dict[str, str] = {}
_current_locale: str = "en"

def load_translations(locale_code: Optional[str] = None) -> None:
    global _translations, _current_locale
    
    if locale_code is None:
        try: # to detect system language
            sys_locale = locale.getdefaultlocale()[0]
            if sys_locale:
                locale_code = sys_locale.split('_')[0]
        except Exception:
            pass
            
    if not locale_code:
        locale_code = "en"
        
    _current_locale = locale_code
    
    if getattr(sys, 'frozen', False):
        base_dir = sys._MEIPASS
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    locales_dir = os.path.join(base_dir, "locales")
    
    file_path = os.path.join(locales_dir, f"{locale_code}.json")
    
    if not os.path.exists(file_path):
        if locale_code != "en":
            load_translations("en")
        return

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            _translations = json.load(f)
    except Exception as e:
        print(f"Error loading translations for {locale_code}: {e}", file=sys.stderr)
        _translations = {}

def _(text: str) -> str:
    return _translations.get(text, text)

def get_current_locale() -> str:
    return _current_locale
