package ops

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type PreflightCheck struct {
	Command    []string `json:"command"`
	ReturnCode int      `json:"returncode"`
	Stdout     string   `json:"stdout"`
	Stderr     string   `json:"stderr"`
	Created    bool     `json:"created,omitempty"`
	Updated    bool     `json:"updated,omitempty"`
	Deleted    bool     `json:"deleted,omitempty"`
}

type PreflightReport struct {
	CWD                           string                    `json:"cwd"`
	UID                           int                       `json:"uid"`
	GID                           int                       `json:"gid"`
	ProcStatus                    map[string]string         `json:"proc_status"`
	UserMaxUserNamespaces         string                    `json:"user_max_user_namespaces,omitempty"`
	KernelUnprivilegedUsernsClone string                    `json:"kernel_unprivileged_userns_clone,omitempty"`
	Checks                        map[string]PreflightCheck `json:"checks"`
	Classification                string                    `json:"classification"`
	NextSteps                     []string                  `json:"next_steps"`
}

func RunPreflight(ctx context.Context) (PreflightReport, error) {
	cwd, _ := os.Getwd()
	report := PreflightReport{
		CWD:                           cwd,
		UID:                           os.Getuid(),
		GID:                           os.Getgid(),
		ProcStatus:                    parseProcStatus(),
		UserMaxUserNamespaces:         readText("/proc/sys/user/max_user_namespaces"),
		KernelUnprivilegedUsernsClone: readText("/proc/sys/kernel/unprivileged_userns_clone"),
		Checks:                        map[string]PreflightCheck{},
		Classification:                "BLOCKED",
	}
	report.Checks["id"] = toPreflightCheck(RunCommand(ctx, "id"))
	report.Checks["unshare_userns"] = toPreflightCheck(RunCommand(ctx, "unshare", "-Ur", "true"))
	bwrap := findFirstExisting("/usr/bin/bwrap", "/bin/bwrap")
	if bwrap == "" {
		report.Checks["bwrap_version"] = PreflightCheck{Command: []string{"bwrap", "--version"}, ReturnCode: 127, Stderr: "bwrap not found"}
		report.Checks["bwrap_userns"] = PreflightCheck{Command: []string{"bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"}, ReturnCode: 127, Stderr: "bwrap not found"}
	} else {
		report.Checks["bwrap_version"] = toPreflightCheck(RunCommand(ctx, bwrap, "--version"))
		report.Checks["bwrap_userns"] = toPreflightCheck(RunCommand(ctx, bwrap, "--unshare-user", "--ro-bind", "/", "/", "true"))
	}
	report.Checks["apply_patch_like_io"] = probeApplyPatchLikeIO()
	classifyPreflight(&report)
	return report, nil
}

func PrintPreflightJSON(report PreflightReport) error {
	payload, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return err
	}
	fmt.Println(string(payload))
	return nil
}

func classifyPreflight(report *PreflightReport) {
	unshareOK := report.Checks["unshare_userns"].ReturnCode == 0
	bwrapOK := report.Checks["bwrap_userns"].ReturnCode == 0
	ioOK := report.Checks["apply_patch_like_io"].ReturnCode == 0
	seccomp := report.ProcStatus["Seccomp"]
	noNewPrivs := report.ProcStatus["NoNewPrivs"]
	if unshareOK && bwrapOK && ioOK {
		report.Classification = "READY"
		report.NextSteps = []string{
			"Run targeted repository tests in the same runtime.",
			"Keep the same container security profile for all agent work.",
		}
		return
	}
	report.NextSteps = []string{
		"If running on Bazzite, Silverblue, Fedora CoreOS, or another immutable host, prefer an unconfined or privileged container profile instead of tuning kernel.unprivileged_userns_clone.",
		"Retry with: podman compose -f docker-compose.yml up",
		"If podman compose is unavailable, retry with: podman-compose -f docker-compose.yml up",
		"For podman run, use --security-opt seccomp=unconfined --security-opt label=disable --userns=host and add --privileged if bwrap remains blocked.",
		"If the host exposes kernel.unprivileged_userns_clone, run sysctl on it; if it returns 0, set it to 1 and reload sysctl before retrying.",
		"Verify the runtime with: bwrap --ro-bind / / true",
		"Do not continue to code generation until unshare and bwrap probes pass in the same runtime.",
	}
	if seccomp == "2" {
		report.NextSteps = append([]string{"The process is under seccomp filtering; investigate container security policy first."}, report.NextSteps...)
	}
	if noNewPrivs == "1" {
		report.NextSteps = append(report.NextSteps[:1], append([]string{"The process has no_new_privileges enabled; this confirms a constrained runtime."}, report.NextSteps[1:]...)...)
	}
	if !bwrapOK {
		report.NextSteps = append(report.NextSteps[:2], append([]string{"bubblewrap is still blocked; the current runtime cannot safely execute sandboxed shell commands or stable apply_patch updates."}, report.NextSteps[2:]...)...)
	}
}

func probeApplyPatchLikeIO() PreflightCheck {
	dir, err := os.MkdirTemp("", "apply-patch-probe")
	if err != nil {
		return PreflightCheck{ReturnCode: 1, Stderr: err.Error()}
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "apply_patch_probe.txt")
	created := os.WriteFile(path, []byte("before\n"), 0o644) == nil
	updated := false
	deleted := false
	if created {
		updated = os.WriteFile(path, []byte("after\n"), 0o644) == nil
	}
	if updated {
		deleted = os.Remove(path) == nil
	}
	code := 1
	if created && updated && deleted {
		code = 0
	}
	return PreflightCheck{ReturnCode: code, Created: created, Updated: updated, Deleted: deleted}
}

func parseProcStatus() map[string]string {
	wanted := map[string]struct{}{"NoNewPrivs": {}, "Seccomp": {}, "Seccomp_filters": {}, "CapPrm": {}, "CapEff": {}, "CapBnd": {}}
	result := map[string]string{}
	content := readText("/proc/self/status")
	for _, line := range strings.Split(content, "\n") {
		key, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		if _, exists := wanted[key]; exists {
			result[key] = strings.TrimSpace(value)
		}
	}
	return result
}

func readText(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func findFirstExisting(paths ...string) string {
	for _, path := range paths {
		if _, err := os.Stat(path); err == nil {
			return path
		}
	}
	return ""
}

func toPreflightCheck(result CommandResult) PreflightCheck {
	return PreflightCheck{Command: result.Command, ReturnCode: result.ReturnCode, Stdout: result.Stdout, Stderr: result.Stderr}
}
