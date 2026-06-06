import random
import re
import json
import itertools

# -------------------- LLM Engines --------------------
class LLM:
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError


class MockLLM(LLM):
    """Runs without a real model; returns valid JSON to let the workflow run end-to-end."""
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        # Route by role keywords
        if "Role: Auditor" in user_prompt:
            return self._audit(user_prompt)
        if "Role: Diagnostician" in user_prompt:
            return self._diagnose(user_prompt)
        return self._candidates(user_prompt)

    def _candidates(self, user_prompt: str) -> str:
        m = re.search(r"Propose\s+(\d+)", user_prompt)
        n = int(m.group(1)) if m else 12

        parent_ids = re.findall(r"-\s+(R\d+-\d+)", user_prompt)
        if not parent_ids:
            parent_ids = ["R?-01"]

        def _parse_factor_levels(name: str, default_vals):
            pat = rf"{re.escape(name)}:\s*\[(.*?)\]"
            hit = re.search(pat, user_prompt)
            if not hit:
                return list(default_vals)
            vals = []
            for raw in hit.group(1).split(','):
                token = raw.strip().strip("'\"")
                if token == "":
                    continue
                vals.append(token)
            return vals or list(default_vals)

        pva_tokens = _parse_factor_levels("pva_wt_percent", ["12.0", "15.0", "18.0"])
        ft_tokens = _parse_factor_levels("freeze_thaw_cycles", ["1", "2", "3"])

        pvas = [float(x) for x in pva_tokens]
        fts = [int(float(x)) for x in ft_tokens]

        combos = list(itertools.product(pvas, fts))
        candidates = []

        for idx, (pva, cycles) in enumerate(combos[:n], start=1):
            parent = parent_ids[(idx - 1) % len(parent_ids)]
            evidence = [
                f"Parent diagnosis required DOE coverage for pva_wt_percent={pva}.",
                f"Parent diagnosis required DOE coverage for freeze_thaw_cycles={cycles}.",
                f"Candidate is generated in phase 1 main DOE coverage before any extension.",
            ]
            lever_text = (
                f"Use parent result {parent} as reference, vary PVA concentration to {pva} wt% "
                f"and freeze-thaw cycle count to {cycles} to complete the mandated DOE grid."
            )
            candidates.append({
                "candidate_id": f"R?-{idx:02d}",
                "parent_candidate_id": parent,
                "parent_candidates": [parent],
                "generation_mode": "result_driven",
                "diagnosis_evidence_used": evidence,
                "mutation_rationale": lever_text,
                "diagnosis_levers_used": ["pva_wt_percent", "freeze_thaw_cycles"],
                "doe_factor_levels": {
                    "pva_wt_percent": str(pva),
                    "freeze_thaw_cycles": str(cycles),
                },
                "doe_factor_levels_used": {
                    "pva_wt_percent": str(pva),
                    "freeze_thaw_cycles": str(cycles),
                },
                "doe_compliance": True,
                "outside_doe_space": False,
                "is_extension": False,
                "extension_reason": "",
                "formulation": {
                    "pva_wt_percent": float(pva),
                    "additives": [],
                    "network_type": "freeze_thaw",
                    "crosslink_or_phys_method": "freeze_thaw",
                },
                "processing": {
                    "steps": [
                        {"order": 1, "name": "Dissolve PVA in DI water at 90 to 95 C until clear", "duration_hours": 2.5},
                        {"order": 2, "name": "Cast and degas", "duration_hours": 0.5},
                        {"order": 3, "name": "Perform freeze-thaw cycles", "duration_hours": float(12 * int(cycles))},
                        {"order": 4, "name": "Post-soak in DI water", "duration_hours": 2.0},
                    ],
                    "freeze_thaw_cycles": int(cycles),
                    "freeze_temp_C": -20,
                    "thaw_temp_C": 25,
                    "cycle_hours": 12,
                    "post_soak_hours": 2,
                    "total_batch_mass_g": 20,
                },
                "materials": [
                    {"name": "PVA", "role": "polymer", "amount": float(pva), "unit": "wt_percent", "basis": "wt%_of_total"},
                    {"name": "DI water", "role": "solvent", "amount": round(100.0 - float(pva), 2), "unit": "wt_percent", "basis": "wt%_of_total"},
                ],
                "expected_mechanism": [
                    f"Reference parent result {parent}; hold material system fixed and cover the mandated DOE point PVA={pva} wt%, freeze-thaw cycles={cycles}.",
                    "PVA concentration tunes network density, swelling resistance, and contact stiffness in DI water.",
                    "Freeze-thaw cycle count tunes crystalline domain density, fatigue resistance, and lubrication stability.",
                ],
                "risks_and_mitigations": [
                    {
                        "risk": "Excess swelling or softening in DI water can cause plowing and debris formation.",
                        "mitigation": "Complete the prescribed PVA and freeze-thaw DOE grid before adding any new material route.",
                    },
                    {
                        "risk": "Too many cycles at high PVA may increase stiffness and friction variability.",
                        "mitigation": "Use the DOE grid to identify the best balance between robustness and low COF.",
                    },
                ],
                "predicted_tradeoff": {
                    "cof_trend": self.rng.choice(["lower", "similar", "higher"]),
                    "wear_trend": self.rng.choice(["lower", "similar", "higher"]),
                    "stability_trend": self.rng.choice(["more_stable", "similar", "less_stable"]),
                    "notes": "Phase-1 DOE candidate. No new materials, no new crosslinker, no extension route.",
                },
                "confidence": round(self.rng.uniform(0.55, 0.85), 2),
            })

        return json.dumps({"candidates": candidates, "missing_info": []}, ensure_ascii=False)

    def _audit(self, user_prompt: str) -> str:
        m = re.search(r"select\s+(\d+)\s+candidates", user_prompt, flags=re.I)
        k = int(m.group(1)) if m else 8
        ids = re.findall(r"\"candidate_id\"\s*:\s*\"(R\d-\d\d)\"", user_prompt)
        # If ids are not embedded, just return empty audits
        audits = [{"candidate_id": cid, "decision": "PASS", "failed_rules": [], "required_fixes": []} for cid in ids]
        selected = ids[:k]
        doe_plan = {
            "factors": [{"name": "freeze_thaw_cycles", "levels": ["low", "high"], "mapping_note": "Cover cycle variation"}],
            "coverage_check": ["Selected candidates span different PVA wt% and cycles."]
        }
        return json.dumps({"audits": audits, "selected_for_round": selected, "doe_plan": doe_plan, "missing_info": []},
                          ensure_ascii=False)

    def _diagnose(self, user_prompt: str) -> str:
        summary = (
            "Primary issues remain DI-water swelling or softening and low-speed lubrication instability. "
            "The next round must first complete the parent-diagnosis DOE on the already selected main levers: "
            "pva_wt_percent and freeze_thaw_cycles. Do not introduce borate ions, glycerol, PEGDA, or any other new material "
            "until the full 12/15/18 wt% by 1/2/3 cycle grid has been covered and reviewed."
        )
        out = {
            "dominant_failure_modes": [
                {
                    "mode": "swelling_softening_then_debris_or_delamination",
                    "evidence": "COF jump, softening, or visible debris under DI-water lubrication.",
                    "affected_candidates": [],
                    "confidence": 0.78,
                },
                {
                    "mode": "boundary_lubrication_instability_or_stick_slip",
                    "evidence": "Low-speed friction fluctuation or unstable steady-state COF.",
                    "affected_candidates": [],
                    "confidence": 0.72,
                },
            ],
            "inferred_mechanisms": [
                {
                    "hypothesis": "PVA concentration is not yet optimized for swelling resistance versus compliance in DI water.",
                    "supporting_evidence": ["Parent diagnosis identified PVA concentration as a main lever."],
                    "contradicting_evidence": [],
                },
                {
                    "hypothesis": "Freeze-thaw cycle count is not yet optimized for crystalline reinforcement versus friction penalty.",
                    "supporting_evidence": ["Parent diagnosis identified freeze-thaw cycle count as a main lever."],
                    "contradicting_evidence": [],
                },
            ],
            "actionable_levers": [
                {
                    "lever": "pva_wt_percent",
                    "direction": "systematically_cover",
                    "rationale": "Complete the required DOE grid at 12.0, 15.0, and 18.0 wt% before changing materials.",
                    "expected_effect_on": {"cof": "tunable", "wear": "tunable", "stability": "tunable"},
                },
                {
                    "lever": "freeze_thaw_cycles",
                    "direction": "systematically_cover",
                    "rationale": "Complete the required DOE grid at 1, 2, and 3 cycles before any extension route.",
                    "expected_effect_on": {"cof": "tunable", "wear": "lower with sufficient reinforcement", "stability": "more_stable"},
                },
            ],
            "next_round_doe": {
                "goal": "Strictly complete the parent-diagnosis main DOE before any material extension.",
                "phase": "main_doe_only",
                "factors": [
                    {
                        "name": "pva_wt_percent",
                        "levels": ["12.0", "15.0", "18.0"],
                        "operational_definition": "PVA wt% in the precursor solution.",
                    },
                    {
                        "name": "freeze_thaw_cycles",
                        "levels": ["1", "2", "3"],
                        "operational_definition": "Number of freeze-thaw cycles at -20 C / 25 C with 12 h per cycle.",
                    },
                ],
                "required_combinations": [
                    {"pva_wt_percent": "12.0", "freeze_thaw_cycles": "1"},
                    {"pva_wt_percent": "12.0", "freeze_thaw_cycles": "2"},
                    {"pva_wt_percent": "12.0", "freeze_thaw_cycles": "3"},
                    {"pva_wt_percent": "15.0", "freeze_thaw_cycles": "1"},
                    {"pva_wt_percent": "15.0", "freeze_thaw_cycles": "2"},
                    {"pva_wt_percent": "15.0", "freeze_thaw_cycles": "3"},
                    {"pva_wt_percent": "18.0", "freeze_thaw_cycles": "1"},
                    {"pva_wt_percent": "18.0", "freeze_thaw_cycles": "2"},
                    {"pva_wt_percent": "18.0", "freeze_thaw_cycles": "3"},
                ],
                "suggested_sample_count": 9,
                "allow_extension": False,
                "extension_gate": {
                    "all_conditions_required": True,
                    "conditions": [
                        "main DOE coverage completed",
                        "main DOE results still do not resolve the critical failure mode",
                        "candidate is explicitly marked is_extension=true",
                        "candidate includes a non-empty extension_reason",
                    ],
                },
            },
            "summary_for_generator": summary,
            "missing_info": [],
        }
        return json.dumps(out, ensure_ascii=False)

class VllmOpenAIChatLLM(LLM):
    """
    Calls vLLM OpenAI-Compatible Server via OpenAI Python client.

    vLLM server implements /v1/chat/completions. :contentReference[oaicite:7]{index=7}
    """
    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "token-abc123",
        model_name: str = "qwen3-14b-sft",
        max_tokens: int = 2048,
        temperature: float = 0.2,
        top_p: float = 0.95,
        timeout_s: float = 120.0,
        extra_body: dict | None = None,
    ):
        from openai import OpenAI
        # base_url should include /v1 as in vLLM docs. :contentReference[oaicite:8]{index=8}
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.timeout_s = timeout_s
        self.extra_body = extra_body or {}

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        # vLLM supports OpenAI Chat Completions API. :contentReference[oaicite:9]{index=9}
        resp = self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
            stream=False,
            # vLLM supports extra params via extra_body. :contentReference[oaicite:10]{index=10}
            extra_body=self.extra_body,
            timeout=self.timeout_s,
        )
        content = resp.choices[0].message.content
        return (content or "").strip()



class TransformersLLM(LLM):
    """Local HuggingFace inference for Qwen3-14B base or SFT checkpoints."""
    def __init__(self, model_path: str, max_new_tokens: int = 2048, temperature: float = 0.2):
        from transformers import AutoTokenizer, AutoModelForCausalLM
        import torch

        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            trust_remote_code=True,
            torch_dtype=getattr(torch, "bfloat16", torch.float16),
            device_map="auto",
        )
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        import torch

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tokenizer(text, return_tensors="pt").to(self.model.device)

        with torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=True,
                temperature=self.temperature,
                top_p=0.95,
            )
        gen = self.tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        return gen.strip()