package localmodels

import "testing"

func TestCandidateEndpointsDoesNotInjectContainerAliasForLoopbackPrimary(t *testing.T) {
	endpoints := candidateEndpoints("http://127.0.0.1:11434", nil)

	assertContains(t, endpoints, "http://127.0.0.1:11434")
	assertContains(t, endpoints, "http://localhost:11434")
	assertNotContains(t, endpoints, "http://host.containers.internal:11434")
}

func TestCandidateEndpointsDerivesLoopbackVariantsFromContainerAlias(t *testing.T) {
	endpoints := candidateEndpoints("http://host.containers.internal:11434", nil)

	assertContains(t, endpoints, "http://host.containers.internal:11434")
	assertContains(t, endpoints, "http://127.0.0.1:11434")
	assertContains(t, endpoints, "http://localhost:11434")
}

func assertContains(t *testing.T, values []string, expected string) {
	t.Helper()
	for _, value := range values {
		if value == expected {
			return
		}
	}
	t.Fatalf("expected %q in %v", expected, values)
}

func assertNotContains(t *testing.T, values []string, unexpected string) {
	t.Helper()
	for _, value := range values {
		if value == unexpected {
			t.Fatalf("did not expect %q in %v", unexpected, values)
		}
	}
}
