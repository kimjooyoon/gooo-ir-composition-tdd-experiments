#!/usr/bin/env python3
"""Stdlib-only integrity and replay checks for the revision-2 cohort."""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]

def sha(data): return hashlib.sha256(data).hexdigest()
def load(path): return json.loads(path.read_text(encoding="utf-8"))
def suite_sha(cases): return "sha256:" + sha(json.dumps(cases, ensure_ascii=False, separators=(",", ":")).encode())

def verify_files(manifest_path, base, files):
    bad=[]
    for rel, expected in files.items():
        p=base/rel
        if not p.is_file() or sha(p.read_bytes()) != expected: bad.append(rel)
    if bad: raise SystemExit("hash mismatch: " + ", ".join(bad[:8]))

def go_test(go, directory):
    env=os.environ.copy()
    env.update({"GOTOOLCHAIN":"local","GOPROXY":"off","GOSUMDB":"off","GOWORK":"off"})
    env.pop("GOOO_LAYA_URL",None); env.pop("GOOO_LAYA_API_KEY",None)
    env["PATH"]=str(go.parent)+os.pathsep+env.get("PATH","")
    return subprocess.run([str(go),"test","-json","-count=1","./..."],cwd=directory,env=env,capture_output=True,text=True,timeout=180,check=False)

def package_status(output, prefix):
    out={}
    for line in output.splitlines():
        try: event=json.loads(line)
        except json.JSONDecodeError: continue
        package=event.get("Package","")
        if (package == prefix or package.startswith(prefix + "/")) and event.get("Action") in {"pass","fail"} and "Test" not in event:
            out[package.rsplit("/",1)[-1]]=event["Action"]
    return out

def python_expected(spec_path, case_id, value):
    spec=importlib.util.spec_from_file_location("revision2_python_spec", spec_path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return int(module.expected(case_id, value))

def check_candidate_outputs(module_dir, catalog, cohort_dir, vectors_path, expected_failures, statuses):
    passed=0; failed=0; unknown=0
    vectors={row["id"]:row for row in load(vectors_path)}
    for index, design in enumerate(catalog["designs"],1):
        plan=load(cohort_dir/design["body_fill_plan"])
        case_vectors=vectors[design["id"]]
        training=case_vectors["training"]
        evaluation=case_vectors["evaluation"]
        for option in plan["candidates"]:
            package=f"case{index:02d}_{design['id']}_{option['id']}"
            status=statuses.get(package)
            path=module_dir/package/"candidate-results.json"
            if status=="pass" and path.is_file():
                passed+=1; rows=load(path)
                expected_rows=[{"split":"training","input":row["input"],"expected":row["expected"]} for row in training]
                expected_rows += [{"split":"evaluation","input":row["input"],"expected":row["expected"]} for row in evaluation]
                actual_rows=[{key:row[key] for key in ("split","input","expected")} for row in rows]
                if actual_rows!=expected_rows: raise SystemExit(f"candidate rows differ from frozen cases: {design['id']}/{option['id']}")
                for row in rows:
                    gold=python_expected(ROOT/"oracle/python_spec.py",design["id"],row["input"])
                    if gold!=row["expected"]: raise SystemExit(f"candidate expected value differs from Python oracle: {design['id']} input {row['input']}")
            elif status=="fail" and (design["id"],option["id"]) in expected_failures:
                failed+=1
                if path.exists(): raise SystemExit(f"invalid candidate unexpectedly emitted results: {design['id']}/{option['id']}")
            elif status is None:
                unknown+=1
            else:
                raise SystemExit(f"unexpected candidate replay status: {design['id']}/{option['id']} {status}")
    return {"passed":passed,"failed":failed,"unknown":unknown}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--replay",action="store_true")
    parser.add_argument("--go-bin",type=Path)
    args=parser.parse_args()
    manifest=load(ROOT/"revision-manifest.json")
    verify_files(ROOT/"revision-manifest.json",ROOT,manifest["files"])
    design=load(ROOT/"design-freeze.json")
    verify_files(ROOT/"design-freeze.json",ROOT,design["files"])
    preparer=REPO/"scripts/prepare_ir_composition_revision2.py"
    if sha(preparer.read_bytes()) != manifest["preparer_sha256"]: raise SystemExit("preparer hash mismatch")
    original_freeze=REPO/"cohorts/ir-composition-curriculum-2026-09-30/design-freeze.json"
    if sha(original_freeze.read_bytes()) != manifest["original_design_freeze_sha256"]: raise SystemExit("original freeze hash mismatch")
    catalog=load(ROOT/"catalog.json"); original=load(REPO/"cohorts/ir-composition-curriculum-2026-09-30/catalog.json")
    ids=[r["id"] for r in catalog["designs"]]
    if len(ids)!=32 or len(set(ids))!=32 or ids != [r["id"] for r in original["designs"]]: raise SystemExit("revision must retain the original 32 intention IDs")
    if len({r["intent"] for r in catalog["designs"]})!=32: raise SystemExit("intention texts must remain unique")
    original_report=load(ROOT/"evaluation/original-candidate-evaluation.json")
    revised_report=load(ROOT/"evaluation/revision2-candidate-evaluation.json")
    if original_report["candidate_denominator"]!=96 or original_report["compiled_candidates"]!=93 or original_report["compile_failed_candidates"]!=3 or original_report["unknown_candidates"]!=0: raise SystemExit("original candidate denominator drift")
    if revised_report["candidate_denominator"]!=96 or revised_report["compiled_candidates"]!=96 or revised_report["compile_failed_candidates"]!=0 or revised_report["unknown_candidates"]!=0: raise SystemExit("revision-2 candidate denominator drift")
    revised_vectors=load(ROOT/"oracle/testdata/vectors.json")
    if revised_report["reference_go_test_exit_code"]!=0 or revised_report["reference_python_case_count"]!=sum(len(r["training"])+len(r["evaluation"]) for r in revised_vectors): raise SystemExit("revision-2 Go/Python reference suite incomplete")
    discrimination=load(ROOT/"evaluation/revision2-candidate-discrimination.json")
    if len(discrimination["designs"])!=32 or not all(r["both_distractors_separated"] and r["gold_passes_all_training"] for r in discrimination["designs"]): raise SystemExit("revision-2 candidate discrimination incomplete")
    feedback_total=0
    vectors={row["id"]:row for row in load(ROOT/"oracle/testdata/vectors.json")}
    for entry in catalog["designs"]:
        base_name=entry["plan_basename"]
        fill=load(ROOT/"plans/body-fill"/base_name)
        vector=vectors[entry["id"]]
        original_suite=vector["training"]
        if fill["test_cases"]!=original_suite: raise SystemExit("body-fill suite differs from frozen vectors")
        for arm in ("legacy-no-feedback","compact-no-feedback","compact-external-feedback"):
            plan=load(ROOT/"plans/laya"/arm/base_name)
            if plan.get("provider_model")!="english" or plan.get("test_cases")!=original_suite: raise SystemExit("plan routing or suite mismatch")
            if plan.get("holdout_test_cases")!=vector["evaluation"]: raise SystemExit("holdout mismatch")
            if arm=="compact-no-feedback" and plan.get("prompt_profile")!="compact": raise SystemExit("compact profile missing")
            if arm=="legacy-no-feedback" and "prompt_profile" in plan: raise SystemExit("legacy prompt profile drift")
            if arm=="compact-external-feedback":
                feedback=plan["external_training_feedback"]
                fixture=(ROOT/entry["fixture"]).read_bytes()
                if feedback["source_digest"]!="sha256:"+sha(fixture): raise SystemExit("feedback source binding mismatch")
                if feedback["training_suite_sha256"]!=suite_sha(original_suite): raise SystemExit("feedback suite binding mismatch")
                expected={row["input"]:row["expected"] for row in original_suite}
                seen=set()
                if feedback["candidate_id"] not in {c["id"] for c in plan["candidates"]}: raise SystemExit("feedback candidate is undeclared")
                for obs in feedback["observations"]:
                    if obs["input"] in seen or obs["input"] not in expected or obs["expected"]!=expected[obs["input"]]: raise SystemExit("feedback observation not uniquely training-bound")
                    if obs["passed"] != (obs["actual"]==obs["expected"]): raise SystemExit("feedback passed flag mismatch")
                    seen.add(obs["input"])
                package_index=ids.index(entry["id"])+1
                actual_path=ROOT/"evaluation/revision2-candidates"/f"case{package_index:02d}_{entry['id']}_{feedback['candidate_id']}"/"candidate-results.json"
                actual_rows=[row for row in load(actual_path) if row["split"]=="training"]
                observed=[{"input":row["input"],"expected":row["expected"],"actual":row["actual"],"passed":row["actual"]==row["expected"]} for row in actual_rows]
                if feedback["observations"]!=observed or {obs["input"] for obs in observed}!=set(expected): raise SystemExit("feedback differs from complete compiled training observations")
                feedback_total+=1
    if feedback_total!=32: raise SystemExit("feedback plan denominator drift")
    if args.replay:
        if not args.go_bin: raise SystemExit("--replay requires --go-bin")
        version=subprocess.run([str(args.go_bin),"version"],capture_output=True,text=True,check=False)
        if version.returncode or "go1.27.0" not in version.stdout: raise SystemExit("replay requires physical Go 1.27.0")
        modules=[
            (ROOT/"evaluation/original-reference","example.invalid/gooo/ir-composition-reference",0),
            (ROOT/"evaluation/revision2-reference","example.invalid/gooo/ir-composition-reference",0),
            (ROOT/"evaluation/original-candidates","example.invalid/gooo/ir-composition-revision2-original",1),
            (ROOT/"evaluation/revision2-candidates","example.invalid/gooo/ir-composition-revision2-revised",0),
        ]
        replay=[]; package_statuses=[]
        for module,prefix,want_exit in modules:
            result=go_test(args.go_bin,module); statuses=package_status(result.stdout,prefix)
            if result.returncode!=want_exit: raise SystemExit(f"unexpected Go exit status in {module.relative_to(ROOT)}: {result.returncode}")
            replay.append({"module":module.relative_to(ROOT).as_posix(),"exit_code":result.returncode,"package_count":len(statuses)})
            package_statuses.append(statuses)
        if replay[0]["package_count"]!=1 or replay[1]["package_count"]!=1: raise SystemExit("reference Go test package missing")
        original_catalog=load(REPO/"cohorts/ir-composition-curriculum-2026-09-30/catalog.json")
        revised_catalog=load(ROOT/"catalog.json")
        invalid={("bl21_saved_range_predicates","option_a"),("bl21_saved_range_predicates","option_c"),("bl23_nonzero_bounded_flag","option_b")}
        old_counts=check_candidate_outputs(ROOT/"evaluation/original-candidates",original_catalog,REPO/"cohorts/ir-composition-curriculum-2026-09-30",REPO/"cohorts/ir-composition-curriculum-2026-09-30/oracle/testdata/vectors.json",invalid,package_statuses[2])
        new_counts=check_candidate_outputs(ROOT/"evaluation/revision2-candidates",revised_catalog,ROOT,ROOT/"oracle/testdata/vectors.json",set(),package_statuses[3])
        if old_counts!={"passed":93,"failed":3,"unknown":0}: raise SystemExit("original candidate replay denominator drift")
        if new_counts!={"passed":96,"failed":0,"unknown":0}: raise SystemExit("revision-2 candidate replay denominator drift")
        if replay[2]["package_count"]!=96 or replay[3]["package_count"]!=96: raise SystemExit("candidate replay package denominator drift")
        print(json.dumps({"validation":"passed","go_version":version.stdout.strip(),"replay":replay,"original_candidate_counts":old_counts,"revision2_candidate_counts":new_counts},indent=2))
    else:
        print(json.dumps({"validation":"passed","designs":32,"original_candidates":96,"revision2_candidates":96,"laya_plans":96,"stdlib_only":True},indent=2))

if __name__=="__main__": main()
