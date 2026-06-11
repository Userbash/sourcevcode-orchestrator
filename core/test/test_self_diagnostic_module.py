import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from core.core.self_diagnostic_module import SelfDiagnosticModule

def test_self_diagnostic_module_initialization():
    module = SelfDiagnosticModule()
    assert module.name == "self_diagnostic"

def test_run_diagnostics_structure():
    module = SelfDiagnosticModule()
    api = MagicMock()
    api.log = MagicMock()
    
    # Mocking necessary API methods
    api.get_context = MagicMock(return_value=MagicMock(modules={}))
    api.get_memory = MagicMock(return_value=MagicMock())
    
    asyncio.run(module.on_load(api))
    report = asyncio.run(module.run_diagnostics())
    
    assert "status" in report
    assert "components" in report
    assert "memory" in report
    assert "ai_models" in report
    assert "timestamp" in report

def test_ai_model_diagnostics():
    module = SelfDiagnosticModule()
    api = MagicMock()
    
    # Mocking availability check
    module._availability = MagicMock()
    module._availability.check_all = MagicMock(return_value={})
    
    asyncio.run(module.on_load(api))
    report = asyncio.run(module.run_diagnostics())
    assert isinstance(report["ai_models"], dict)
