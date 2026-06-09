import pytest
from pathlib import Path
import json
from core.core.autodev_pipeline_module import AutodevPipelineModule

def test_pipeline_enforces_tdd_cycle_react_no_tests(tmp_path: Path):
    pipeline = AutodevPipelineModule()
    # Create package.json to simulate React project
    (tmp_path / "package.json").write_text(json.dumps({"name": "test-app", "scripts": {"test": "echo 'fail' && exit 1"}}))
    
    specs = "Implement a Button component"
    result = pipeline.run_pipeline(specs, tmp_path)
    assert result["status"] == "failed"
    assert "No failing test found" in str(result.get("state", {}).get("errors", [{}])[0])

def test_pipeline_enforces_tdd_cycle_react_red_tests(tmp_path: Path):
    pipeline = AutodevPipelineModule()
    # Create package.json
    (tmp_path / "package.json").write_text(json.dumps({"name": "test-app", "scripts": {"test": "exit 1"}}))
    # Create a failing React test
    (tmp_path / "Button.test.tsx").write_text("import { render } from '@testing-library/react'; test('renders button', () => { throw new Error('Not implemented'); });")
    
    specs = "Implement a Button component"
    result = pipeline.run_pipeline(specs, tmp_path)
    
    # Should NOT fail the TDD check (since we have a failing test)
    errors = result.get("state", {}).get("errors", [])
    tdd_errors = [e for e in errors if "No failing test" in str(e) or "Tests are already GREEN" in str(e)]
    assert len(tdd_errors) == 0

def test_pipeline_enforces_tdd_cycle_react_green_tests(tmp_path: Path):
    pipeline = AutodevPipelineModule()
    # Create package.json with a passing test script
    (tmp_path / "package.json").write_text(json.dumps({"name": "test-app", "scripts": {"test": "exit 0"}}))
    # Create a test file
    (tmp_path / "Button.test.tsx").write_text("test('renders', () => {})")
    
    specs = "Implement a Button component"
    result = pipeline.run_pipeline(specs, tmp_path)
    
    # Should fail because tests are already GREEN
    assert result["status"] == "failed"
    assert "Tests are already GREEN" in str(result.get("state", {}).get("errors", [{}])[0])
