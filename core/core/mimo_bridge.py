import json
import subprocess
from dataclasses import dataclass
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)

@dataclass
class MimoModel:
    full_id: str
    id: str
    provider: str
    status: str
    context_window: Optional[int]

class MimoBridge:
    def get_models(self) -> List[MimoModel]:
        try:
            output = subprocess.check_output(["mimo", "models", "--verbose"]).decode('utf-8')
            return self._parse_models_output(output)
        except subprocess.SubprocessError as e:
            logger.error(f"Error executing mimo models: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error getting mimo models: {e}")
            return []

    def _parse_models_output(self, output: str) -> List[MimoModel]:
        if not output.strip():
            return []
        
        results = []
        lines = output.strip().split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
                
            # The first line of a block is the full model ID
            full_id = line
            i += 1
            
            # The following lines are a JSON object
            json_str = ""
            brace_count = 0
            in_json = False
            
            while i < len(lines):
                j_line = lines[i]
                json_str += j_line + "\n"
                i += 1
                
                if "{" in j_line:
                    brace_count += j_line.count("{")
                    in_json = True
                if "}" in j_line:
                    brace_count -= j_line.count("}")
                    
                if in_json and brace_count <= 0:
                    break
                    
            if json_str.strip():
                try:
                    data = json.loads(json_str)
                    results.append(MimoModel(
                        full_id=full_id,
                        id=data.get("id", ""),
                        provider=data.get("providerID", ""),
                        status=data.get("status", ""),
                        context_window=data.get("limit", {}).get("context")
                    ))
                except json.JSONDecodeError:
                    pass
                    
        return results
