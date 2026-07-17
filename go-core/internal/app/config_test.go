package app

import "testing"

func TestLoadConfigPrefersExplicitAddressAndStatePath(t *testing.T) {
	t.Setenv("GO_CORE_ADDR", " 127.0.0.1:9090 ")
	t.Setenv("GO_CORE_STATE_PATH", " /tmp/go-core-state ")

	cfg := LoadConfig()
	if cfg.Addr != "127.0.0.1:9090" {
		t.Fatalf("LoadConfig Addr = %q, want 127.0.0.1:9090", cfg.Addr)
	}
	if cfg.StatePath != "/tmp/go-core-state" {
		t.Fatalf("LoadConfig StatePath = %q, want /tmp/go-core-state", cfg.StatePath)
	}
	if len(cfg.RequiredHTTPEndpoints) != len(DefaultRequiredHTTPEndpoints) {
		t.Fatalf("LoadConfig RequiredHTTPEndpoints len = %d, want %d", len(cfg.RequiredHTTPEndpoints), len(DefaultRequiredHTTPEndpoints))
	}
}

func TestResolveAddrFallsBackToHostAndPort(t *testing.T) {
	t.Setenv("GO_CORE_ADDR", "")
	t.Setenv("AI_BRIDGE_HTTP_ADDR", "")
	t.Setenv("GO_CORE_HOST", " 10.0.0.5 ")
	t.Setenv("AI_BRIDGE_API_PORT", " 18080 ")

	if got := resolveAddr(); got != "10.0.0.5:18080" {
		t.Fatalf("resolveAddr() = %q, want 10.0.0.5:18080", got)
	}
}

func TestResolvePostgresConnectionInfoBuildsEscapedURL(t *testing.T) {
	t.Setenv("AI_BRIDGE_MEMORY_DATABASE_URL", "")
	t.Setenv("AI_BRIDGE_POSTGRES_USER", "svc user")
	t.Setenv("AI_BRIDGE_POSTGRES_PASSWORD", "p@ss word")
	t.Setenv("AI_BRIDGE_POSTGRES_DB", "core")
	t.Setenv("AI_BRIDGE_POSTGRES_HOST", "db.internal")
	t.Setenv("AI_BRIDGE_POSTGRES_PORT", "5433")

	info := ResolvePostgresConnectionInfo()
	if info.URL != "postgresql://svc+user:p%40ss+word@db.internal:5433/core?sslmode=disable" {
		t.Fatalf("ResolvePostgresConnectionInfo URL = %q", info.URL)
	}
}

func TestResolveRabbitMQConnectionInfoUsesDefaults(t *testing.T) {
	t.Setenv("AI_BRIDGE_RABBITMQ_USER", "")
	t.Setenv("RABBITMQ_DEFAULT_USER", "")
	t.Setenv("AI_BRIDGE_RABBITMQ_PASSWORD", "")
	t.Setenv("RABBITMQ_DEFAULT_PASS", "")
	t.Setenv("AI_BRIDGE_RABBITMQ_HOST", "")
	t.Setenv("RABBITMQ_HOST", "")
	t.Setenv("AI_BRIDGE_RABBITMQ_PORT", "")
	t.Setenv("RABBITMQ_PORT", "")
	t.Setenv("AI_BRIDGE_RABBITMQ_URL", "")

	info := ResolveRabbitMQConnectionInfo()
	if info.User != "guest" {
		t.Fatalf("ResolveRabbitMQConnectionInfo User = %q, want guest", info.User)
	}
	if info.Host != "127.0.0.1" {
		t.Fatalf("ResolveRabbitMQConnectionInfo Host = %q, want 127.0.0.1", info.Host)
	}
	if info.Port != "5672" {
		t.Fatalf("ResolveRabbitMQConnectionInfo Port = %q, want 5672", info.Port)
	}
	if info.AMQPURL != "amqp://guest:@127.0.0.1:5672/" {
		t.Fatalf("ResolveRabbitMQConnectionInfo AMQPURL = %q", info.AMQPURL)
	}
	if info.ManagementURL != "http://127.0.0.1:15672" {
		t.Fatalf("ResolveRabbitMQConnectionInfo ManagementURL = %q", info.ManagementURL)
	}
}
