from pathlib import Path
from typing import Any, Dict, List, Tuple
import json
import re
import csv

# -------------------- Utils --------------------
def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _to_float_or_none(x):
    if x is None:
        return None
    try:
        s = str(x).strip()
        if s == "" or s.lower() in {"na", "nan", "none"}:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None

def load_allowed_materials(csv_path: Path):
    """Load allowed materials from a CSV with columns: Name_en, CAS, role, functional_groups.

    Returns (names_list, info_dict) where info_dict maps lowercase name to metadata.
    """
    materials = []
    material_info: Dict[str, Dict[str, str]] = {}
    try:
        with csv_path.open(encoding="utf-8", errors="ignore") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return [], {}
            for row in reader:
                name = (row.get("Name_en") or row.get(reader.fieldnames[0]) or "").strip()
                if not name:
                    continue
                low = name.lower()
                role = (row.get("role") or "").strip()
                fg = (row.get("functional_groups") or "").strip()
                materials.append(name)
                material_info[low] = {"name": name, "role": role, "functional_groups": fg}
    except FileNotFoundError:
        return [], {}
    # unique, preserve order
    seen = set()
    out = []
    for m in materials:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out, material_info


# ---- Material Name Canonicalization (Plan D) ----
# Maps common LLM-generated material name variants to standardized canonical names.
# Applied early in post-processing so downstream checks see consistent names.
# IMPORTANT: canonical names MUST match exact entries in materials_en.csv.
MATERIAL_NAME_CANONICAL: Dict[str, str] = {
    # Glutaraldehyde variants
    "50 wt% 戊二醛溶液": "Glutaraldehyde",
    "50 wt% glutaraldehyde solution": "Glutaraldehyde",
    "glutaraldehyde (50 wt%)": "Glutaraldehyde",
    "ga": "Glutaraldehyde",
    # HCl variants → CSV: "Hydrochloric acid (HCl)"
    "36%到38% 浓盐酸": "Hydrochloric acid (HCl)",
    "36-38% hydrochloric acid": "Hydrochloric acid (HCl)",
    "concentrated hydrochloric acid": "Hydrochloric acid (HCl)",
    "hydrochloric acid": "Hydrochloric acid (HCl)",
    "hcl (37%)": "Hydrochloric acid (HCl)",
    "hcl": "Hydrochloric acid (HCl)",
    # Sodium Hyaluronate variants → CSV: "Sodium hyaluronate"
    "透明质酸钠 sodium hyaluronate": "Sodium hyaluronate",
    "透明质酸钠": "Sodium hyaluronate",
    "sodium hyaluronate (ha)": "Sodium hyaluronate",
    "hyaluronic acid sodium salt": "Sodium hyaluronate",
    "ha": "Sodium hyaluronate",
    "sodium hyaluronate": "Sodium hyaluronate",
    "Sodium Hyaluronate": "Sodium hyaluronate",
    # CMC variants → CSV: "Carboxymethyl cellulose (CMC)"
    "羧甲基纤维素钠 cmc": "Carboxymethyl cellulose (CMC)",
    "羧甲基纤维素钠": "Carboxymethyl cellulose (CMC)",
    "sodium carboxymethyl cellulose": "Carboxymethyl cellulose (CMC)",
    "carboxymethyl cellulose sodium": "Carboxymethyl cellulose (CMC)",
    "carboxymethyl cellulose": "Carboxymethyl cellulose (CMC)",
    "cmc": "Carboxymethyl cellulose (CMC)",
    # DMSO variants → CSV: "DMSO"
    "二甲基亚砜 dmso": "DMSO",
    "二甲基亚砜": "DMSO",
    "dimethyl sulfoxide": "DMSO",
    "dmso": "DMSO",
    # PVA variants → CSV: "PVA (polyvinyl alcohol)"
    "polyvinyl alcohol": "PVA (polyvinyl alcohol)",
    "pva (polyvinyl alcohol)": "PVA (polyvinyl alcohol)",
    "聚乙烯醇": "PVA (polyvinyl alcohol)",
    "pva": "PVA (polyvinyl alcohol)",
    # Water → CSV: not in CSV, but always allowed by role="solvent"
    "di water": "DI water",
    "deionized water": "DI water",
    "去离子水": "DI water",
    "ultrapure water": "DI water",
    # Mucin → CSV: not in CSV
    "胃黏蛋白 gastric mucin": "Gastric Mucin",
    "mucin": "Gastric Mucin",
    "gastric mucin": "Gastric Mucin",
    # Acrylamide → CSV: "Acrylamide"
    "丙烯酰胺 acrylamide": "Acrylamide",
    "丙烯酰胺": "Acrylamide",
    # MBAA → CSV: "N,N-Methylenebisacrylamide"
    "n,n′-亚甲基双丙烯酰胺 mbaa": "N,N-Methylenebisacrylamide",
    "n,n-methylene bisacrylamide": "N,N-Methylenebisacrylamide",
    "mbaa": "N,N-Methylenebisacrylamide",
    # Photoinitiator → CSV: "2-Hydroxy-2-methylpropiophenone"
    "2-羟基-2-甲基-1-苯基-1-丙酮": "2-Hydroxy-2-methylpropiophenone",
    "darocur 1173": "2-Hydroxy-2-methylpropiophenone",
    "2-hydroxy-2-methylpropiophenone": "2-Hydroxy-2-methylpropiophenone",
    # PEGDMA → CSV: "PEG dimethacrylate"
    "聚乙二醇二甲基丙烯酸酯 pegdm": "PEG dimethacrylate",
    "poly(ethylene glycol) dimethacrylate": "PEG dimethacrylate",
    "pegdma": "PEG dimethacrylate",
    # NVP → CSV: "N-Vinyl-2-pyrrolidone"
    "n-乙烯基吡咯烷酮 nvp": "N-Vinyl-2-pyrrolidone",
    "n-vinylpyrrolidone": "N-Vinyl-2-pyrrolidone",
    "nvp": "N-Vinyl-2-pyrrolidone",
    # DMAAm → CSV: "N,N-Dimethylacrylamide"
    "n,n-二甲基丙烯酰胺 dmaam": "N,N-Dimethylacrylamide",
    "n,n-dimethylacrylamide": "N,N-Dimethylacrylamide",
    "dmaam": "N,N-Dimethylacrylamide",
    # APTAC → not in CSV, keep as is
    "3-丙烯酰胺丙基三甲基氯化铵 aptac": "APTAC",
    "aptac": "APTAC",
    # Irgacure 2959 → CSV: "2-Hydroxy-2-methylpropiophenone" (same as Darocur)
    "irgacure 2959": "2-Hydroxy-2-methylpropiophenone",
    # Glycerol → CSV: "Glycerol"
    "glycerol": "Glycerol",
    "甘油": "Glycerol",
    # PEG → CSV: "PEG (polyethylene glycol)"
    "peg": "PEG (polyethylene glycol)",
    "polyethylene glycol": "PEG (polyethylene glycol)",
    "聚乙二醇": "PEG (polyethylene glycol)",
    # Epoxy → CSV: not explicitly in CSV as "Epoxy Resin"
    "epoxy resin": "Epoxy Resin",
    "epoxy": "Epoxy Resin",
    # Photo-initiator generic → remove
    "photo-initiator": "__REMOVE__",
    "photoinitiator": "__REMOVE__",
    # Generic placeholder patterns to remove
    "none": "__REMOVE__",
    "n/a": "__REMOVE__",
    "null": "__REMOVE__",
}


def canonicalize_material_name(name: str) -> str:
    """Normalize a material name to its canonical form.

    Returns the canonical name, or the original if no mapping exists.
    Returns '__REMOVE__' for placeholder names that should be removed.
    """
    if not name:
        return name
    stripped = name.strip()
    # Exact match first
    if stripped in MATERIAL_NAME_CANONICAL:
        return MATERIAL_NAME_CANONICAL[stripped]
    # Case-insensitive match
    low = stripped.lower()
    for key, value in MATERIAL_NAME_CANONICAL.items():
        if key.lower() == low:
            return value
    # Substring match (longer key contained in name)
    for key, value in sorted(MATERIAL_NAME_CANONICAL.items(), key=lambda x: -len(x[0])):
        if len(key) >= 4 and key.lower() in low:
            return value
    return stripped


def canonicalize_candidate_materials(candidate: dict) -> list[str]:
    """Apply material name canonicalization to all materials in a candidate dict.
    Returns list of corrections made (for logging).
    """
    corrections: list[str] = []
    materials = candidate.get("materials") or []
    for m in materials:
        if not isinstance(m, dict):
            continue
        old_name = (m.get("name") or "").strip()
        if not old_name:
            continue
        new_name = canonicalize_material_name(old_name)
        if new_name == "__REMOVE__":
            corrections.append(f"REMOVED placeholder: '{old_name}'")
            m["name"] = ""
        elif new_name != old_name:
            corrections.append(f"'{old_name}' -> '{new_name}'")
            m["name"] = new_name

    # Also fix formulation crosslinker/initiator names
    f = candidate.get("formulation") or {}
    for key in ("crosslinker", "initiator_or_catalyst", "photo_initiator", "nanofiller"):
        sub = f.get(key) or {}
        if isinstance(sub, dict) and sub.get("name"):
            old = sub["name"].strip()
            new = canonicalize_material_name(old)
            if new == "__REMOVE__":
                sub["name"] = ""
                corrections.append(f"formulation.{key}.name REMOVED placeholder: '{old}'")
            elif new != old:
                sub["name"] = new
                corrections.append(f"formulation.{key}.name: '{old}' -> '{new}'")

    # Fix additive names in formulation
    for a in (f.get("additives") or []):
        if isinstance(a, dict) and a.get("name"):
            old = a["name"].strip()
            new = canonicalize_material_name(old)
            if new == "__REMOVE__":
                a["name"] = ""
                corrections.append(f"additive REMOVED placeholder: '{old}'")
            elif new != old:
                a["name"] = new
                corrections.append(f"additive: '{old}' -> '{new}'")

    return corrections


def find_similar_materials(
    target_name: str,
    target_role: str,
    material_info: Dict[str, Dict[str, str]],
    top_n: int = 3,
) -> List[Dict[str, Any]]:
    """Find chemically similar allowed materials to a rejected/proposed material.

    Args:
        target_name: the material name that was rejected (e.g., "Polyvinylpyrrolidone")
        target_role: the intended role (e.g., "adhesive_additive", "plasticizer")
        material_info: dict from load_allowed_materials
        top_n: max suggestions to return

    Returns list of {name, role, functional_groups, score, reason}
    """
    target_low = target_name.lower().strip()

    # Keyword-to-functional-group mapping for common materials not in CSV
    KNOWN_EQUIVALENTS: Dict[str, Dict[str, Any]] = {
        "polyvinylpyrrolidone": {"tags": "amide|pyrrolidone|hydrophilic|biocompatible", "role": "secondary_polymer"},
        "pvp": {"tags": "amide|pyrrolidone|hydrophilic|biocompatible", "role": "secondary_polymer"},
        "dmso": {"tags": "sulfoxide|polar_aprotic|solvent|penetration_enhancer", "role": "solvent"},
        "n,n-dimethylformamide": {"tags": "amide|polar_aprotic|solvent", "role": "solvent"},
        "acrylic acid": {"tags": "carboxyl|vinyl|monomer|pH_sensitive", "role": "secondary_polymer"},
        "polyacrylamide": {"tags": "amide|hydrogel|hydrophilic", "role": "secondary_polymer"},
        "poly(n-isopropylacrylamide)": {"tags": "amide|thermo_responsive|hydrogel", "role": "secondary_polymer"},
        "pnipam": {"tags": "amide|thermo_responsive|hydrogel", "role": "secondary_polymer"},
        "lubricant": {"tags": "lubricant|biocompatible|polysaccharide|friction_reduction", "role": "lubricant_additive"},
        "lubricant additive": {"tags": "lubricant|biocompatible|polysaccharide|friction_reduction", "role": "lubricant_additive"},
        "nanofiller": {"tags": "nanoparticle|reinforcement|filler", "role": "nanofiller"},
        "nanoclay": {"tags": "aluminosilicate|layered|hydrophilic|nanoparticle", "role": "nanofiller"},
        "filler additive": {"tags": "nanoparticle|reinforcement|filler", "role": "nanofiller"},
        "filler": {"tags": "nanoparticle|reinforcement|filler", "role": "nanofiller"},
        "reinforcement": {"tags": "nanoparticle|reinforcement|filler", "role": "nanofiller"},
        "adhesive additive": {"tags": "adhesive|binding|film_forming", "role": "secondary_polymer"},
        "borax": {"tags": "borate|crosslinker|pH_sensitive|hydrogen_bonding", "role": "crosslinker"},
        "sodium tetraborate": {"tags": "borate|crosslinker|pH_sensitive|hydrogen_bonding", "role": "crosslinker"},
        "peg": {"tags": "hydroxyl|polyether|water_soluble|plasticizer", "role": "plasticizer"},
    }

    target_info = KNOWN_EQUIVALENTS.get(target_low, {"tags": "", "role": target_role})
    target_tags = set((target_info.get("tags") or "").lower().split("|"))
    target_role_norm = (target_info.get("role") or target_role or "").lower().strip()

    scored = []
    for name_low, info in material_info.items():
        score = 0.0
        reasons = []

        # 1) Role match
        mat_role = (info.get("role") or "").lower()
        if target_role_norm and mat_role:
            if target_role_norm == mat_role:
                score += 3.0
                reasons.append("same_role")
            elif any(r in mat_role for r in target_role_norm.split("_")) or \
                 any(r in target_role_norm for r in mat_role.split("_")):
                score += 1.5
                reasons.append("partial_role_match")

        # 2) Functional group overlap
        mat_fg = set((info.get("functional_groups") or "").lower().split("|"))
        if target_tags and mat_fg:
            overlap = target_tags & mat_fg
            if overlap:
                score += len(overlap) * 1.0
                reasons.append(f"shared_groups:{','.join(sorted(overlap)[:3])}")

        # 3) Name substring match
        target_words = set(target_low.replace("(", " ").replace(")", " ").replace(",", " ").split())
        mat_words = set(name_low.replace("(", " ").replace(")", " ").replace(",", " ").split())
        word_overlap = target_words & mat_words - {"the", "a", "an", "of", "and", "or", "in", "on", "to", "for"}
        if word_overlap:
            score += len(word_overlap) * 2.0
            reasons.append(f"name_overlap:{','.join(sorted(word_overlap)[:3])}")

        # 4) If target contains the material name or vice versa
        if target_low in name_low or name_low in target_low:
            score += 2.0
            reasons.append("name_contains")

        if score > 0:
            scored.append({
                "name": info["name"],
                "role": info.get("role", ""),
                "functional_groups": info.get("functional_groups", ""),
                "score": round(score, 2),
                "reasons": reasons,
            })

    scored.sort(key=lambda x: -x["score"])
    return scored[:top_n]

def write_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def _salvage_candidates_from_text(text: str):
    """
    针对形如 {"candidates": [ {...}, {...}, {...未完}] 的截断输出：
    尽量解析 candidates 数组前面完整的元素，丢掉后面不完整的。
    """
    m = re.search(r'"candidates"\s*:\s*\[', text)
    if not m:
        return None

    start = m.end()  # 在 '[' 之后
    arr_src = text[start:]
    dec = json.JSONDecoder()
    idx = 0
    length = len(arr_src)
    candidates = []

    while True:
        # 跳过空白和逗号
        while idx < length and arr_src[idx] in " \t\r\n,":
            idx += 1
        if idx >= length or arr_src[idx] == ']':
            break

        try:
            obj, next_idx = dec.raw_decode(arr_src, idx)
        except json.JSONDecodeError:
            # 这里通常就是遇到最后一个不完整的 candidate，直接停止
            break

        candidates.append(obj)
        idx = next_idx

    if not candidates:
        return None

    return {"candidates": candidates}

def safe_json_loads(s: str):
    """Extract the first JSON object from model output, best-effort."""
    # 去掉前后空白
    s = s.strip()
    # 去掉 Qwen 的 <think>...</think> 思考段
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()

    # 1) 先尝试整个字符串就是 JSON
    if s.startswith("{") and s.endswith("}"):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass

    # 2) 提取从第一个 { 到最后一个 } 的块
    m = re.search(r"\{.*\}", s, flags=re.S)
    if m:
        text = m.group(0)
    else:
        m2 = re.search(r"\{.*", s, flags=re.S)
        if not m2:
            raise ValueError("No JSON object found in model output.")
        text = m2.group(0)

    # 2a) 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # 2b) 去掉对象 / 数组结尾处多余逗号
        cleaned = re.sub(r",(\s*[}\]])", r"\1", text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # 2c) 修复常见“少逗号”情况：
            #   * 对象之间: "}{"
            #   * 字段之间: "]\n  \"key\"" 或 "}\n  \"key\""
            fixed = re.sub(r"([}\]])(?=\s*[\{\"])", r"\1,", cleaned)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                # 2d) 仅对生成器输出：尝试从 candidates 里挽救前面完整元素
                salvaged = _salvage_candidates_from_text(fixed)
                if salvaged is not None:
                    return salvaged
                # 还是不行，就把原始错误抛出去
                raise e



# Re-exports from bruker_parser (kept for backward compatibility)
from .bruker_parser import (
    parse_bruker_csv, get_step_data, detect_half_cycles,
    discriminate_pattern, parse_compression_csv,
    compute_compression_modulus, extract_cof_stats_from_bruker,
    build_results_from_bruker_csvs, plot_fx_vs_t,
)
