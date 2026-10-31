"""Record unified sources; preserve provenance instead of reimporting the retired two-plugin layout."""
from pathlib import Path
import hashlib
import json
ROOT = Path(__file__).resolve().parents[1]

def record_sources(root=ROOT):
    from build_release import files
    manifest_path = root / 'source-manifest.json'
    prior = json.loads(manifest_path.read_text('utf-8')) if manifest_path.exists() else {}
    old = {row['path']: row for row in prior.get('files', [])}
    rows = []
    prefix = 'plugins/astrbot_plugin_livingmemory/'
    legacy = 'plugins/astrbot_plugin_self_learning/'
    for path in files(root / 'plugins'):
        rel = path.relative_to(root).as_posix()
        before_path = rel
        if rel.startswith(prefix + 'learning/'):
            tail = rel[len(prefix + 'learning/'):]
            before_path = legacy + {'module.py': 'main.py', 'UPSTREAM_METADATA.yaml': 'metadata.yaml'}.get(tail, tail)
        elif rel.startswith(prefix + 'pages/learning/'):
            before_path = legacy + 'pages/dashboard/' + rel[len(prefix + 'pages/learning/'):]
        previous = old.get(rel) if prior.get('release') == '1.1.0' else old.get(before_path)
        previous = previous or {}
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        baseline = previous.get('baseline_release_sha256') if prior.get('release') == '1.1.0' else previous.get('release_sha256')
        rows.append({'path': rel, 'baseline_path': previous.get('baseline_path', before_path),
                     'source_sha256': previous.get('source_sha256'), 'baseline_release_sha256': baseline,
                     'release_sha256': digest, 'modified': digest != previous.get('source_sha256'),
                     'changed_from_1_0_1': digest != baseline})
    manifest = {'release': '1.1.0', 'date': '2026-10-31',
                'baseline': '1.0.1 maintained source; single Star with internal learning module', 'files': rows}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return manifest

if __name__ == '__main__':
    print(json.dumps({'source_files': len(record_sources()['files'])}))
