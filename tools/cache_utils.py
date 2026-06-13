"""Persistent disk cache for API responses across process restarts.

Usage:
    from tools.cache_utils import cached, get, set

    @cached("exchange_data", ttl_seconds=30)
    def fetch_price(symbol: str):
        ...

    # Or manual:
    result = get("exchange_data", "price", "BTC", ttl_seconds=30)
    if result is None:
        result = api_call()
        set("exchange_data", "price", "BTC", result=result)
"""

import json
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Optional, Callable
from functools import wraps

# Default cache root under <workspace>/cache/api_responses
CACHE_ROOT = Path(__file__).parent.parent / "cache" / "api_responses"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


def _cache_dir(tool_name: str) -> Path:
    d = CACHE_ROOT / tool_name
    d.mkdir(exist_ok=True)
    return d


def _make_key(*parts: Any) -> str:
    key = "|".join(str(p) for p in parts)
    return hashlib.md5(key.encode()).hexdigest()


def get(tool_name: str, *key_parts: Any, ttl_seconds: int = 60) -> Optional[Any]:
    """Fetch from cache if not expired."""
    cache_file = _cache_dir(tool_name) / f"{_make_key(*key_parts)}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        if time.time() - data.get("_cached_at", 0) > ttl_seconds:
            return None
        return data.get("result")
    except Exception:
        return None


def set(tool_name: str, *key_parts: Any, result: Any):
    """Store result in cache."""
    cache_file = _cache_dir(tool_name) / f"{_make_key(*key_parts)}.json"
    try:
        cache_file.write_text(json.dumps({
            "_cached_at": time.time(),
            "result": result
        }, default=str))
    except Exception:
        pass  # Fail silently — cache is best-effort


def clear_expired(tool_name: str = None, max_age_seconds: int = None):
    """Clear expired cache entries. If tool_name is None, cleans all tools.
    
    Args:
        tool_name: Specific tool to clean, or None for all
        max_age_seconds: Delete files older than this (regardless of TTL).
                        If None, only deletes if file is older than its own TTL
                        (requires reading each file's _cached_at + original TTL).
                        Use 86400 for "older than 1 day", 604800 for "older than 1 week".
    
    Returns:
        Dict with deleted count and bytes freed
    """
    import glob
    
    deleted = 0
    bytes_freed = 0
    
    targets = [CACHE_ROOT / tool_name] if tool_name else [d for d in CACHE_ROOT.iterdir() if d.is_dir()]
    
    for d in targets:
        for cache_file in d.glob("*.json"):
            try:
                # If max_age specified, use it as hard cutoff
                if max_age_seconds:
                    mtime = cache_file.stat().st_mtime
                    if time.time() - mtime > max_age_seconds:
                        size = cache_file.stat().st_size
                        cache_file.unlink()
                        deleted += 1
                        bytes_freed += size
                else:
                    # Check if expired by reading _cached_at
                    # We can't know original TTL, so use file mtime as proxy
                    # Files not accessed in >2x their likely TTL are stale
                    data = json.loads(cache_file.read_text())
                    cached_at = data.get("_cached_at", 0)
                    # Conservative: if older than 24h, it's stale for any reasonable TTL
                    if time.time() - cached_at > 86400:
                        size = cache_file.stat().st_size
                        cache_file.unlink()
                        deleted += 1
                        bytes_freed += size
            except Exception:
                pass
    
    return {"deleted": deleted, "bytes_freed": bytes_freed}


def get_cache_stats() -> Dict:
    """Get cache usage stats across all tools."""
    import glob
    
    stats = {}
    total_files = 0
    total_bytes = 0
    
    for d in CACHE_ROOT.iterdir():
        if not d.is_dir():
            continue
        files = list(d.glob("*.json"))
        size = sum(f.stat().st_size for f in files)
        stats[d.name] = {
            "files": len(files),
            "size_kb": round(size / 1024, 1)
        }
        total_files += len(files)
        total_bytes += size
    
    stats["_total"] = {
        "files": total_files,
        "size_kb": round(total_bytes / 1024, 1),
        "size_mb": round(total_bytes / (1024 * 1024), 2)
    }
    return stats


def clear_all():
    """Clear ALL cache entries across all tools."""
    for d in CACHE_ROOT.iterdir():
        if d.is_dir():
            for f in d.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass


def clear_expired(tool_name: str = None, max_age_seconds: int = None):
    """Clear expired cache entries. If tool_name is None, cleans all tools.
    
    Args:
        tool_name: Specific tool to clean, or None for all
        max_age_seconds: Delete files older than this (regardless of TTL).
                        If None, deletes files older than 24h.
    
    Returns:
        Dict with deleted count and bytes freed
    """
    deleted = 0
    bytes_freed = 0
    
    targets = [CACHE_ROOT / tool_name] if tool_name else [d for d in CACHE_ROOT.iterdir() if d.is_dir()]
    
    for d in targets:
        for cache_file in d.glob("*.json"):
            try:
                if max_age_seconds:
                    mtime = cache_file.stat().st_mtime
                    if time.time() - mtime > max_age_seconds:
                        size = cache_file.stat().st_size
                        cache_file.unlink()
                        deleted += 1
                        bytes_freed += size
                else:
                    data = json.loads(cache_file.read_text())
                    cached_at = data.get("_cached_at", 0)
                    if time.time() - cached_at > 86400:
                        size = cache_file.stat().st_size
                        cache_file.unlink()
                        deleted += 1
                        bytes_freed += size
            except Exception:
                pass
    
    return {"deleted": deleted, "bytes_freed": bytes_freed}


def get_cache_stats() -> Dict:
    """Get cache usage stats across all tools."""
    stats = {}
    total_files = 0
    total_bytes = 0
    
    for d in CACHE_ROOT.iterdir():
        if not d.is_dir():
            continue
        files = list(d.glob("*.json"))
        size = sum(f.stat().st_size for f in files)
        stats[d.name] = {
            "files": len(files),
            "size_kb": round(size / 1024, 1)
        }
        total_files += len(files)
        total_bytes += size
    
    stats["_total"] = {
        "files": total_files,
        "size_kb": round(total_bytes / 1024, 1),
        "size_mb": round(total_bytes / (1024 * 1024), 2)
    }
    return stats


def clear_all():
    """Clear ALL cache entries across all tools."""
    for d in CACHE_ROOT.iterdir():
        if d.is_dir():
            for f in d.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass


def clear(tool_name: str):
    """Clear all cache entries for a tool."""
    d = _cache_dir(tool_name)
    for f in d.glob("*.json"):
        f.unlink()


# ─── CLI ───
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Cache management for API responses")
    sub = parser.add_subparsers(dest="command", help="Command")
    
    # stats
    sub.add_parser("stats", help="Show cache usage stats")
    
    # clear
    clear_parser = sub.add_parser("clear", help="Clear cache for a tool")
    clear_parser.add_argument("tool", nargs="?", help="Tool name (or omit for all)")
    clear_parser.add_argument("--age", type=int, help="Delete files older than N seconds")
    
    # clear-all
    sub.add_parser("clear-all", help="Clear ALL cache entries")
    
    args = parser.parse_args()
    
    if args.command == "stats":
        stats = get_cache_stats()
        for tool, info in stats.items():
            if tool == "_total":
                print(f"\nTOTAL: {info['files']} files, {info['size_kb']} KB ({info['size_mb']} MB)")
            else:
                print(f"{tool}: {info['files']} files, {info['size_kb']} KB")
    
    elif args.command == "clear":
        if args.tool:
            result = clear_expired(args.tool, max_age_seconds=args.age)
        else:
            result = clear_expired(max_age_seconds=args.age)
        print(f"Deleted {result['deleted']} files, freed {result['bytes_freed'] / 1024:.1f} KB")
    
    elif args.command == "clear-all":
        clear_all()
        print("All cache cleared")
    
    else:
        parser.print_help()


def cached(tool_name: str, ttl_seconds: int = 60):
    """Decorator for caching function results persistently.
    
    Args:
        tool_name: Unique name for the cache subdirectory
        ttl_seconds: How long to keep cached results
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Build cache key from function name + args (skip 'self' for methods) + sorted kwargs
            key_parts = [func.__name__]
            # Skip first arg if it looks like 'self' (has the method)
            start_idx = 1 if len(args) > 0 and hasattr(args[0], func.__name__) else 0
            key_parts += list(args[start_idx:])
            if kwargs:
                key_parts += [f"{k}={v}" for k, v in sorted(kwargs.items())]
            
            # Try cache first
            cached_result = get(tool_name, *key_parts, ttl_seconds=ttl_seconds)
            if cached_result is not None:
                return cached_result
            
            # Call function and cache result
            result = func(*args, **kwargs)
            set(tool_name, *key_parts, result=result)
            return result
        return wrapper
    return decorator
