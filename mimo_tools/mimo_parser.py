import json

def parse_mimo_models_output(output: str) -> list[dict]:
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
                results.append({
                    "full_id": full_id,
                    "id": data.get("id"),
                    "provider": data.get("providerID"),
                    "status": data.get("status"),
                    "context_window": data.get("limit", {}).get("context")
                })
            except json.JSONDecodeError:
                pass
                
    return results
