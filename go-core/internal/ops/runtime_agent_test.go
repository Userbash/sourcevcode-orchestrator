package ops

import (
	"context"
	"strings"
	"testing"
)

func TestRunRuntimeAgentRejectsInvalidOptionsBeforeExecution(t *testing.T) {
	tests := []struct {
		name    string
		options RuntimeAgentOptions
		want    string
	}{
		{
			name:    "engine",
			options: RuntimeAgentOptions{Engine: "nerdctl", Security: "unconfined", Image: "busybox"},
			want:    "unsupported runtime engine",
		},
		{
			name:    "security",
			options: RuntimeAgentOptions{Engine: "docker", Security: "sandboxed", Image: "busybox"},
			want:    "unsupported runtime security mode",
		},
		{
			name:    "image",
			options: RuntimeAgentOptions{Engine: "docker", Security: "unconfined"},
			want:    "runtime image or executable is required",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := RunRuntimeAgent(context.Background(), tc.options)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("RunRuntimeAgent() error = %v, want substring %q", err, tc.want)
			}
		})
	}
}

func TestRuntimeCandidatesAndDisplayName(t *testing.T) {
	podman := runtimeCandidates("podman", "unconfined")
	if len(podman) != 1 || podman[0].Engine != "podman" || podman[0].Security != "unconfined" {
		t.Fatalf("runtimeCandidates(podman) = %#v", podman)
	}

	docker := runtimeCandidates("docker", "privileged")
	if len(docker) != 1 || docker[0].Engine != "docker" || docker[0].Security != "privileged" {
		t.Fatalf("runtimeCandidates(docker) = %#v", docker)
	}

	auto := runtimeCandidates("auto", "unconfined")
	if len(auto) != 2 || auto[0].Engine != "podman" || auto[1].Engine != "docker" {
		t.Fatalf("runtimeCandidates(auto) = %#v", auto)
	}

	if got := runtimeDisplayName("podman"); got != "Podman" {
		t.Fatalf("runtimeDisplayName(podman) = %q", got)
	}
	if got := runtimeDisplayName("docker"); got != "Docker" {
		t.Fatalf("runtimeDisplayName(docker) = %q", got)
	}
	if got := runtimeDisplayName("custom"); got != "custom" {
		t.Fatalf("runtimeDisplayName(custom) = %q", got)
	}
}

func TestCommandArgsWrapHostExecutionOnlyWhenNeeded(t *testing.T) {
	wrapped := commandArgs("echo", "ok")
	if len(wrapped) < 1 {
		t.Fatalf("commandArgs() returned empty slice")
	}
	if commandName() == "flatpak-spawn" {
		if len(wrapped) != 3 || wrapped[0] != "--host" || wrapped[1] != "echo" || wrapped[2] != "ok" {
			t.Fatalf("commandArgs() wrapped = %#v", wrapped)
		}
		return
	}
	if len(wrapped) != 2 || wrapped[0] != "echo" || wrapped[1] != "ok" {
		t.Fatalf("commandArgs() direct = %#v", wrapped)
	}
}
