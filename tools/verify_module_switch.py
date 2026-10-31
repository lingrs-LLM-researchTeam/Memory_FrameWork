"""Exercise combined -> memory-only -> combined across real isolated core restarts."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    import verify_startup as startup
    startup.prepare()
    config = startup.RUNTIME / 'data/config/astrbot_plugin_livingmemory_config.json'
    steps = [('memory_and_learning', 1), ('memory_only', 1), ('memory_and_learning', 2)]
    results = []
    for index, (mode, phase) in enumerate(steps):
        payload = json.loads(config.read_text('utf-8-sig'))
        payload['framework_modules'] = {'mode': mode}
        config.write_text(json.dumps(payload), 'utf-8')
        env = {**os.environ, 'MEMORY_FRAMEWORK_MODE': mode, 'PYTHONIOENCODING': 'utf-8'}
        if index == 2:
            env['MEMORY_FRAMEWORK_SWITCH_CHECK'] = '1'
        with (startup.ROOT / f'switch-{index}.log').open('w', encoding='utf-8') as log:
            run = subprocess.run([sys.executable, '-B', startup.__file__, '--child', str(phase)],
                                 env=env, stdout=log, stderr=subprocess.STDOUT, timeout=200)
        if run.returncode:
            raise RuntimeError(f'Module switch step {index} failed; inspect switch-{index}.log')
        result = json.loads((startup.ROOT / f'startup-phase-{phase}.json').read_text())
        result['mode'] = mode
        result['reenabled_source_reconciled'] = index == 2
        results.append(result)
    (startup.ROOT / 'module-switch-results.json').write_text(json.dumps(results, indent=2), 'utf-8')
    print(json.dumps(results))


if __name__ == '__main__':
    main()
