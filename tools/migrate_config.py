"""Merge existing plugin configurations into a NEW file; never touch running data."""
import argparse
from copy import deepcopy
import json
from pathlib import Path


def merge_configs(memory, learning):
    if not isinstance(memory, dict) or not isinstance(learning, dict):
        raise ValueError('Both configuration roots must be JSON objects')
    result = deepcopy(memory)

    def merge(target, source, prefix=''):
        for key, value in source.items():
            if key not in target:
                target[key] = deepcopy(value)
            elif isinstance(target[key], dict) and isinstance(value, dict):
                merge(target[key], value, prefix + key + '.')
            elif target[key] != value:
                raise ValueError('Conflicting configuration field: ' + prefix + key)
    merge(result, learning)
    result.setdefault('framework_modules', {}).setdefault('mode', 'memory_and_learning')
    return result


def migrate(memory_path, learning_path, output):
    memory_path, learning_path, output = (Path(p).resolve() for p in (memory_path, learning_path, output))
    if output in (memory_path, learning_path):
        raise ValueError('Output must be a new file, not either input')
    result = merge_configs(json.loads(memory_path.read_text('utf-8-sig')),
                           json.loads(learning_path.read_text('utf-8-sig')))
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return {'output': str(output), 'inputs_unchanged': True, 'database_migration': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory', type=Path, required=True)
    parser.add_argument('--learning', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.memory, args.learning, args.output), ensure_ascii=False))
