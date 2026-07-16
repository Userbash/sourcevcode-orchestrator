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

func main() {
	listenAddr := getenv("LISTEN_ADDR", ":80")
	upstreamBaseURL := getenv("UPSTREAM_BASE_URL", "http://go_core:8010")

	target, err := url.Parse(upstreamBaseURL)
	if err != nil {
		log.Fatalf("invalid UPSTREAM_BASE_URL %q: %v", upstreamBaseURL, err)
	}

	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.FlushInterval = -1
	proxy.ErrorLog = log.New(os.Stderr, "chat-proxy: ", log.LstdFlags)
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		log.Printf("proxy error for %s %s: %v", r.Method, r.URL.String(), err)
		http.Error(w, "upstream unavailable", http.StatusBadGateway)
	}

	originalDirector := proxy.Director
	proxy.Director = func(r *http.Request) {
		originalDirector(r)
		incomingHost := r.Host
		r.Host = target.Host
		r.Header.Set("X-Forwarded-Host", incomingHost)
		r.Header.Set("X-Forwarded-Proto", forwardedProto(r))
		appendForwardedFor(r)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.Handle("/", proxy)

	server := &http.Server{
		Addr:              listenAddr,
		Handler:           requestLogger(mux),
		ReadHeaderTimeout: 10 * time.Second,
	}

	log.Printf("listening on %s and proxying to %s", listenAddr, target.String())
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server failed: %v", err)
	}
}

func getenv(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func requestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("%s %s", r.Method, r.URL.String())
		next.ServeHTTP(w, r)
	})
}

func forwardedProto(r *http.Request) string {
	if r.TLS != nil {
		return "https"
	}
	return "http"
}

func appendForwardedFor(r *http.Request) {
	if ip := clientIP(r.RemoteAddr); ip != "" {
		if prior := r.Header.Get("X-Forwarded-For"); prior != "" {
			r.Header.Set("X-Forwarded-For", prior+", "+ip)
			return
		}
		r.Header.Set("X-Forwarded-For", ip)
	}
}

func clientIP(remoteAddr string) string {
	host := remoteAddr
	if idx := strings.LastIndex(remoteAddr, ":"); idx > 0 {
		host = remoteAddr[:idx]
	}
	return strings.Trim(host, "[]")
}
