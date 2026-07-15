package ops

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

type RuntimeAgentOptions struct {
	Engine      string
	Security    string
	Workspace   string
	Image       string
	CommandArgs []string
}

type BwrapAwareRuntimeOptions struct {
	Workspace               string
	Image                   string
	CommandArgs             []string
	RuntimePreference       string
	AllowPrivilegedFallback bool
}

func RunRuntimeAgent(ctx context.Context, options RuntimeAgentOptions) error {
	engine := strings.TrimSpace(options.Engine)
	security := strings.TrimSpace(options.Security)
	workspace := strings.TrimSpace(options.Workspace)
	image := strings.TrimSpace(options.Image)

	if engine != "docker" && engine != "podman" {
		return fmt.Errorf("unsupported runtime engine %q", engine)
	}
	if security != "privileged" && security != "unconfined" {
		return fmt.Errorf("unsupported runtime security mode %q", security)
	}
	if image == "" {
		return fmt.Errorf("runtime image or executable is required")
	}
	if workspace == "" {
		cwd, err := os.Getwd()
		if err != nil {
			return err
		}
		workspace = cwd
	}

	args := []string{"run", "--rm", "-it"}
	switch {
	case engine == "docker" && security == "privileged":
		args = append(args, "--privileged")
	case engine == "docker" && security == "unconfined":
		args = append(args, "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined")
	case engine == "podman" && security == "privileged":
		args = append(args, "--privileged")
	case engine == "podman" && security == "unconfined":
		args = append(args, "--security-opt", "seccomp=unconfined", "--security-opt", "label=disable")
	}

	mount := workspace + ":/workspace"
	if engine == "podman" {
		mount += ":Z"
	}
	args = append(args, "-v", mount, "-w", "/workspace", image)
	args = append(args, options.CommandArgs...)
	return runStreamingCommand(ctx, engine, args...)
}

func RunBwrapAwareRuntime(ctx context.Context, options BwrapAwareRuntimeOptions) error {
	if probeLocalBwrap(ctx) {
		fmt.Fprintln(os.Stderr, "local bwrap probe passed; using current runtime")
		return runStreamingDirect(ctx, options.Image, options.CommandArgs...)
	}

	fmt.Fprintln(os.Stderr, "local bwrap probe failed; creating a temporary runtime that can execute bwrap")

	preference := strings.TrimSpace(options.RuntimePreference)
	if preference == "" {
		preference = "auto"
	}
	if preference != "auto" && preference != "podman" && preference != "docker" {
		return fmt.Errorf("unsupported runtime preference %q", preference)
	}

	for _, candidate := range runtimeCandidates(preference, "unconfined") {
		if runtimeAvailable(ctx, candidate.Engine) {
			fmt.Fprintf(os.Stderr, "starting temporary %s runtime with %s\n", runtimeDisplayName(candidate.Engine), candidate.Security)
			return RunRuntimeAgent(ctx, RuntimeAgentOptions{
				Engine:      candidate.Engine,
				Security:    candidate.Security,
				Workspace:   options.Workspace,
				Image:       options.Image,
				CommandArgs: options.CommandArgs,
			})
		}
	}

	if options.AllowPrivilegedFallback {
		for _, candidate := range runtimeCandidates(preference, "privileged") {
			if runtimeAvailable(ctx, candidate.Engine) {
				fmt.Fprintf(os.Stderr, "unconfined runtime unavailable; falling back to temporary privileged %s runtime\n", runtimeDisplayName(candidate.Engine))
				return RunRuntimeAgent(ctx, RuntimeAgentOptions{
					Engine:      candidate.Engine,
					Security:    candidate.Security,
					Workspace:   options.Workspace,
					Image:       options.Image,
					CommandArgs: options.CommandArgs,
				})
			}
		}
	}

	return fmt.Errorf(`unable to create a bwrap-capable temporary runtime

expected one of the following:
- local runtime where "bwrap --unshare-user --ro-bind / / true" succeeds
- podman available for "--security-opt seccomp=unconfined --security-opt label=disable"
- docker available for "--security-opt seccomp=unconfined --security-opt apparmor=unconfined"
- privileged fallback explicitly allowed

run "orchestrator runtime-preflight" for detailed diagnostics`)
}

func runStreamingDirect(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = os.Environ()
	return cmd.Run()
}

func runStreamingCommand(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, commandName(), commandArgs(name, args...)...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = os.Environ()
	return cmd.Run()
}

func probeLocalBwrap(ctx context.Context) bool {
	if _, err := exec.LookPath("bwrap"); err != nil {
		return false
	}
	cmd := exec.CommandContext(ctx, "bwrap", "--unshare-user", "--ro-bind", "/", "/", "true")
	return cmd.Run() == nil
}

func runtimeAvailable(ctx context.Context, engine string) bool {
	result := RunCommand(ctx, engine, "--version")
	return result.ReturnCode == 0
}

func runtimeCandidates(preference string, security string) []RuntimeAgentOptions {
	switch preference {
	case "podman":
		return []RuntimeAgentOptions{{Engine: "podman", Security: security}}
	case "docker":
		return []RuntimeAgentOptions{{Engine: "docker", Security: security}}
	default:
		return []RuntimeAgentOptions{
			{Engine: "podman", Security: security},
			{Engine: "docker", Security: security},
		}
	}
}

func runtimeDisplayName(engine string) string {
	switch engine {
	case "podman":
		return "Podman"
	case "docker":
		return "Docker"
	default:
		return engine
	}
}
