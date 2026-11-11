"""Check runtime dependencies against the explicit Docker COPY allowlist."""
import ast
from pathlib import Path
import unittest

class DeploymentManifestTests(unittest.TestCase):
    def test_all_local_imports_including_lazy_ones_are_packaged(self):
        root=Path(__file__).parent
        docker=(root/'Dockerfile').read_text()
        copy=next(line for line in docker.splitlines() if line.startswith('COPY '))
        included={Path(name).stem for name in copy.split()[1:-1] if name.endswith('.py')}
        local={p.stem for p in root.glob('*.py')}
        for module in included:
            tree=ast.parse((root/(module+'.py')).read_text())
            dependencies=set()
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):
                    dependencies.update(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node,ast.ImportFrom) and node.module:
                    dependencies.add(node.module.split('.')[0])
            self.assertFalse((dependencies & local)-included,
                f'{module}: unpackaged runtime dependencies {(dependencies & local)-included}')
        self.assertIn('report_quotes',included)
        run=next(line for line in docker.splitlines() if line.startswith('RUN '))
        self.assertIn('report_quotes',run)
