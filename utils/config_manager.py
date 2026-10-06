import os
import re
import json
from pathlib import Path
from typing import List, Tuple
from utils.logger import logger

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
ENV_FILE = BASE_DIR / ".env"

def parse_key_string(raw: str) -> List[str]:
    """Parse comma, semicolon, or newline delimited API keys."""
    if not raw:
        return []
    keys = []
    for item in re.split(r"[\n,;\r]+", str(raw)):
        k = item.strip().strip('"').strip("'")
        if k and k not in keys:
            keys.append(k)
    return keys

def get_gemini_api_keys() -> List[str]:
    """
    Get all configured Gemini API keys (supporting multiple accounts).
    Looks in environment variables, config.json, and .env.
    """
    keys: List[str] = []

    # 1. Environment variable
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val:
        for k in parse_key_string(env_val):
            if k not in keys:
                keys.append(k)

    # 2. config.json
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Check list
                raw_list = data.get("gemini_api_keys", [])
                if isinstance(raw_list, list):
                    for k in raw_list:
                        k_clean = str(k).strip()
                        if k_clean and k_clean not in keys:
                            keys.append(k_clean)
                # Check single string
                single = data.get("gemini_api_key", "")
                if single:
                    for k in parse_key_string(single):
                        if k not in keys:
                            keys.append(k)
        except Exception as e:
            logger.debug(f"Error reading config.json: {e}")

    # 3. .env file
    if ENV_FILE.exists():
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        val = line.split("=", 1)[1].strip()
                        for k in parse_key_string(val):
                            if k not in keys:
                                keys.append(k)
        except Exception as e:
            pass

    # Prioritize official Google AI Studio keys (AIzaSy...)
    keys.sort(key=lambda k: 0 if k.startswith("AIzaSy") else 1)
    return keys

def get_gemini_api_key() -> str:
    """Return the primary Gemini API key for backward compatibility."""
    keys = get_gemini_api_keys()
    return keys[0] if keys else ""

def save_gemini_api_keys(keys: List[str]) -> bool:
    """
    Save list of Gemini API keys persistently to config.json and .env.
    """
    clean_keys = []
    for k in keys:
        for single in parse_key_string(k):
            if single and single not in clean_keys:
                clean_keys.append(single)

    if not clean_keys:
        return False

    joined = ",".join(clean_keys)
    os.environ["GEMINI_API_KEY"] = joined

    # Save to config.json
    try:
        data = {}
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data["gemini_api_keys"] = clean_keys
        data["gemini_api_key"] = joined
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving to config.json: {e}")

    # Save to .env
    try:
        env_lines = []
        found = False
        if ENV_FILE.exists():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("GEMINI_API_KEY="):
                        env_lines.append(f"GEMINI_API_KEY={joined}\n")
                        found = True
                    else:
                        env_lines.append(line)
        if not found:
            env_lines.append(f"GEMINI_API_KEY={joined}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(env_lines)
    except Exception as e:
        logger.error(f"Error saving to .env: {e}")

    # Synchronize to ai-audio-translator/.env
    try:
        react_env = BASE_DIR / "ai-audio-translator" / ".env"
        if react_env.parent.exists():
            react_lines = []
            react_found = False
            if react_env.exists():
                with open(react_env, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip().startswith("GEMINI_API_KEY="):
                            react_lines.append(f"GEMINI_API_KEY={clean_keys[0]}\n")
                            react_found = True
                        else:
                            react_lines.append(line)
            if not react_found:
                react_lines.append(f"GEMINI_API_KEY={clean_keys[0]}\n")
                react_lines.append("PORT=3000\n")
            with open(react_env, "w", encoding="utf-8") as f:
                f.writelines(react_lines)
    except Exception as e:
        logger.error(f"Error syncing to ai-audio-translator/.env: {e}")

    logger.info(f"🔑 {len(clean_keys)} Gemini API Key(s) saved persistently across Desktop and React Engine!")
    return True

def save_gemini_api_key(key: str) -> bool:
    """Save single or multiple keys passed as string."""
    return save_gemini_api_keys(parse_key_string(key))

def test_gemini_api_key(key: str) -> Tuple[bool, str]:
    """
    Verify if a Gemini API key is valid by testing generateContent on active production models.
    Returns (True, "Success message") or (False, "Error message").
    """
    clean_key = (key or "").strip()
    if not clean_key:
        return False, "API Key is empty."

    import requests
    candidate_models = ["gemini-3.1-flash-lite", "gemini-flash-lite-latest"]
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
    }
    payload = {"contents": [{"parts": [{"text": "ping"}]}]}

    last_code = 0
    last_err = ""
    for model in candidate_models:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={clean_key}"
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            logger.info(f"🔑 [Key Test] {model} -> HTTP {resp.status_code}")
            if resp.status_code == 200:
                return True, f"✅ Key ({clean_key[:6]}...): ដំណើរការជោគជ័យ Active ({model})"
            last_code = resp.status_code
            try:
                data = resp.json()
                last_err = data.get("error", {}).get("message", "")
            except Exception:
                last_err = resp.text[:120]
            logger.warning(f"🔑 [Key Test] {model} failed: HTTP {resp.status_code} - {last_err[:80]}")
            if "not allowed by policy" in last_err.lower() or "not allowed by policy" in resp.text.lower():
                return False, f"⚠️ បណ្តាញអ៊ីនធឺណិតត្រូវបានរារាំង (Network blocked by sandbox/policy). សូម Restart កម្មវិធីក្រៅ Sandbox (Full Network Access)!"
            if resp.status_code == 429:
                return True, f"⚠️ Key ({clean_key[:6]}...): Active (Currently Rate Limited HTTP 429, wait 30s)"
            elif resp.status_code == 400:
                return False, f"❌ Key ({clean_key[:6]}...): មិនត្រឹមត្រូវ (API_KEY_INVALID: {last_err})"
        except Exception as e:
            logger.warning(f"🔑 [Key Test] {model} exception: {e}")
            last_err = str(e)
            continue

    # Fallback check: test model listing endpoint
    try:
        url_models = f"https://generativelanguage.googleapis.com/v1beta/models?key={clean_key}"
        r_models = requests.get(url_models, headers=headers, timeout=15)
        if r_models.status_code == 200:
            return True, f"✅ Key ({clean_key[:6]}...): ដំណើរការជោគជ័យ Active"
        if "not allowed by policy" in r_models.text.lower():
            return False, f"⚠️ បណ្តាញអ៊ីនធឺណិតត្រូវបានរារាំង (Network blocked by sandbox/policy). សូម Restart កម្មវិធីក្រៅ Sandbox (Full Network Access)!"
    except Exception:
        pass

    return False, f"❌ Key ({clean_key[:6]}...): គ្មានសិទ្ធិ (HTTP {last_code}: {last_err[:80]})"
