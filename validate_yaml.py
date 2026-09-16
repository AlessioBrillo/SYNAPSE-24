import yaml
with open('.github/workflows/ci.yml') as f:
    data = yaml.safe_load(f)
print('YAML is valid')
print('Jobs:', list(data.get('jobs', {}).keys()))