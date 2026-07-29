package e2e_test

import (
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func TestSmokeBinaryBuildsAndServesHealth(t *testing.T) {
	if testing.Short() || os.Getenv("RUN_E2E") != "1" {
		t.Skip("set RUN_E2E=1 outside a socket-restricted sandbox")
	}
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	binary := filepath.Join(t.TempDir(), "orchestrator")
	cmd := exec.Command("go", "build", "-o", binary, "./cmd/orchestrator")
	cmd.Dir = root
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("daemon must build: %v\n%s", err, output)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	listener.Close()
	daemon := exec.Command(binary)
	daemon.Env = append(daemon.Environ(), "PORT="+strconv.Itoa(port))
	if err := daemon.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = daemon.Process.Kill(); _ = daemon.Wait() })
	deadline := time.NewTimer(2 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		response, err := http.Get("http://127.0.0.1:" + strconv.Itoa(port) + "/health")
		if err == nil {
			response.Body.Close()
			if response.StatusCode == http.StatusOK {
				return
			}
			t.Fatalf("health = %d", response.StatusCode)
		}
		select {
		case <-deadline.C:
			t.Fatalf("daemon did not become healthy: %v", err)
		case <-ticker.C:
		}
	}
}
