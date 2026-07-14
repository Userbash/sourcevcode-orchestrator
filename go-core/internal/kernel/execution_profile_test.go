package kernel

import "testing"

func TestDetectExecutionProfileHonorsParallelismCap(t *testing.T) {
	t.Setenv("GO_CORE_MAX_PARALLELISM", "2")
	profile := detectExecutionProfile()
	if profile.UsableParallelism != 2 {
		t.Fatalf("expected usable parallelism 2, got %d", profile.UsableParallelism)
	}
}
