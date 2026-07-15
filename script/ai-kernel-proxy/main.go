package main

import (
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"time"
)

func getenv(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func main() {
	listenAddr := getenv("LISTEN_ADDR", ":8012")
	upstreamBaseURL := getenv("UPSTREAM_BASE_URL", "http://host.containers.internal:8012")
	upstreamAPIKey := strings.TrimSpace(os.Getenv("UPSTREAM_API_KEY"))

	target, err := url.Parse(upstreamBaseURL)
	if err != nil {
		log.Fatalf("parse upstream url: %v", err)
	}

	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.Transport = &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		MaxIdleConns:          100,
		IdleConnTimeout:       90 * time.Second,
		ResponseHeaderTimeout: 5 * time.Minute,
		ExpectContinueTimeout: 1 * time.Second,
	}
	originalDirector := proxy.Director
	proxy.Director = func(req *http.Request) {
		originalDirector(req)
		req.Host = target.Host
		if upstreamAPIKey != "" {
			req.Header.Set("Authorization", "Bearer "+upstreamAPIKey)
		}
		req.Header.Set("X-Forwarded-Host", req.Host)
		req.Header.Set("X-Forwarded-Proto", "http")
	}

	http.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	http.Handle("/", proxy)

	log.Printf("ai-kernel proxy listening on %s -> %s", listenAddr, target.String())
	if err := http.ListenAndServe(listenAddr, nil); err != nil {
		log.Fatalf("listen: %v", err)
	}
}
