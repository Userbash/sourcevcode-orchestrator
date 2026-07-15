package app

import (
	"fmt"
	"net/url"
	"os"
	"strings"
)

type PostgresConnectionInfo struct {
	User     string
	Password string
	Database string
	Host     string
	Port     string
	URL      string
}

type RabbitMQConnectionInfo struct {
	User          string
	Password      string
	Host          string
	Port          string
	ManagementURL string
	AMQPURL       string
}

func ResolvePostgresConnectionInfo() PostgresConnectionInfo {
	info := PostgresConnectionInfo{
		User:     firstNonEmptyEnv("AI_BRIDGE_POSTGRES_USER", "POSTGRES_USER"),
		Password: firstNonEmptyEnv("AI_BRIDGE_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
		Database: firstNonEmptyEnv("AI_BRIDGE_POSTGRES_DB", "POSTGRES_DB"),
		Host:     firstNonEmptyEnv("AI_BRIDGE_POSTGRES_HOST", "POSTGRES_HOST"),
		Port:     firstNonEmptyEnv("AI_BRIDGE_POSTGRES_PORT", "POSTGRES_PORT"),
	}
	if info.Database == "" {
		info.Database = "ai_bridge"
	}
	if info.Host == "" {
		info.Host = "127.0.0.1"
	}
	if info.Port == "" {
		info.Port = "5432"
	}
	info.URL = strings.TrimSpace(os.Getenv("AI_BRIDGE_MEMORY_DATABASE_URL"))
	if info.URL == "" && info.User != "" {
		info.URL = fmt.Sprintf(
			"postgresql://%s:%s@%s:%s/%s?sslmode=disable",
			url.QueryEscape(info.User),
			url.QueryEscape(info.Password),
			info.Host,
			info.Port,
			info.Database,
		)
	}
	return info
}

func ResolveRabbitMQConnectionInfo() RabbitMQConnectionInfo {
	info := RabbitMQConnectionInfo{
		User:     firstNonEmptyEnv("AI_BRIDGE_RABBITMQ_USER", "RABBITMQ_DEFAULT_USER"),
		Password: firstNonEmptyEnv("AI_BRIDGE_RABBITMQ_PASSWORD", "RABBITMQ_DEFAULT_PASS"),
		Host:     firstNonEmptyEnv("AI_BRIDGE_RABBITMQ_HOST", "RABBITMQ_HOST"),
		Port:     firstNonEmptyEnv("AI_BRIDGE_RABBITMQ_PORT", "RABBITMQ_PORT"),
	}
	if info.User == "" {
		info.User = "guest"
	}
	if info.Host == "" {
		info.Host = "127.0.0.1"
	}
	if info.Port == "" {
		info.Port = "5672"
	}
	info.AMQPURL = strings.TrimSpace(os.Getenv("AI_BRIDGE_RABBITMQ_URL"))
	if info.AMQPURL == "" {
		info.AMQPURL = fmt.Sprintf(
			"amqp://%s:%s@%s:%s/",
			url.QueryEscape(info.User),
			url.QueryEscape(info.Password),
			info.Host,
			info.Port,
		)
	}
	info.ManagementURL = fmt.Sprintf("http://%s:15672", info.Host)
	return info
}
