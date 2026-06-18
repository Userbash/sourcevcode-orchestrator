import http from 'node:http';
import https from 'node:https';

/**
 * Orchestrator API verification tests for the current AI stack.
 *
 * Checks:
 * - Core health endpoints
 * - Full health payload shape
 * - Antigravity status route reachability
 * - Response headers and 404 handling
 */

class BackendVerificationTest {
    constructor(baseUrl = process.env.ORCHESTRATOR_BASE_URL || 'http://127.0.0.1:8000') {
        this.baseUrl = baseUrl.replace(/\/$/, '');
        this.results = {
            total_tests: 0,
            passed: 0,
            failed: 0,
            errors: [],
            endpoints: []
        };
        this.colors = {
            reset: '\x1b[0m',
            bright: '\x1b[1m',
            green: '\x1b[32m',
            yellow: '\x1b[33m',
            red: '\x1b[31m',
            cyan: '\x1b[36m'
        };
        this.timeout = 10000;
    }

    log(message, color = 'reset') {
        const c = this.colors[color] || '';
        console.log(`${c}${message}${this.colors.reset}`);
    }

    async makeRequest(endpoint, options = {}) {
        const timeoutMs = options.timeoutMs ?? this.timeout;
        const requestOptions = { ...options };
        delete requestOptions.timeoutMs;

        try {
            const url = new URL(`${this.baseUrl}${endpoint}`);
            const transport = url.protocol === 'https:' ? https : http;
            const method = requestOptions.method || 'GET';
            const headers = {
                Accept: 'application/json',
                ...(requestOptions.headers || {})
            };

            const response = await new Promise((resolve, reject) => {
                const req = transport.request(url, { method, headers }, (res) => {
                    const chunks = [];
                    res.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
                    res.on('end', () => {
                        const headerMap = new Map(
                            Object.entries(res.headers).map(([key, value]) => [
                                key.toLowerCase(),
                                Array.isArray(value) ? value.join(', ') : String(value || '')
                            ])
                        );
                        resolve({
                            ok: (res.statusCode || 0) >= 200 && (res.statusCode || 0) < 300,
                            status: res.statusCode || 0,
                            statusText: res.statusMessage || '',
                            headers: {
                                get(name) {
                                    return headerMap.get(String(name).toLowerCase()) || null;
                                }
                            },
                            body: Buffer.concat(chunks).toString('utf-8')
                        });
                    });
                });

                req.setTimeout(timeoutMs, () => {
                    req.destroy(new Error(`Request timeout after ${timeoutMs}ms`));
                });
                req.on('error', reject);
                if (requestOptions.body) {
                    req.write(requestOptions.body);
                }
                req.end();
            });

            return {
                ok: response.ok,
                status: response.status,
                statusText: response.statusText,
                headers: response.headers,
                json: async () => JSON.parse(response.body),
                text: async () => response.body
            };
        } catch (error) {
            return {
                ok: false,
                status: 0,
                statusText: error.message,
                headers: { get() { return null; } },
                json: async () => ({ error: error.message }),
                text: async () => error.message
            };
        }
    }

    async testEndpoint(name, endpoint, expectedStatus = 200, options = {}) {
        this.results.total_tests++;
        this.log(`\n  Testing: ${name}`, 'bright');
        this.log(`  GET ${endpoint}`, 'cyan');

        const response = await this.makeRequest(endpoint, options);
        const contentType = response.headers.get('content-type') || '';
        const payload = contentType.includes('application/json')
            ? await response.json()
            : await response.text();

        if (response.status === expectedStatus) {
            this.log(`  ✓ Status ${response.status} OK`, 'green');
            this.results.passed++;
            this.results.endpoints.push({
                name,
                endpoint,
                status: response.status,
                success: true,
                contentType
            });
            return payload;
        }

        this.log(`  ✗ Expected ${expectedStatus}, got ${response.status}`, 'red');
        this.results.failed++;
        this.results.errors.push({
            test: name,
            expected: expectedStatus,
            actual: response.status,
            body: typeof payload === 'string' ? payload.slice(0, 200) : payload
        });
        return null;
    }

    async waitForBackend(maxAttempts = 20) {
        this.log('Waiting for orchestrator to be ready...', 'cyan');
        for (let i = 0; i < maxAttempts; i++) {
            const response = await this.makeRequest('/health');
            if (response.ok) {
                this.log('✓ Orchestrator is ready!\n', 'green');
                return true;
            }
            await new Promise(resolve => setTimeout(resolve, 1000));
        }
        this.log('✗ Orchestrator did not become ready in time', 'red');
        return false;
    }

    async runTests() {
        this.log('\n' + '═'.repeat(70), 'cyan');
        this.log('  ORCHESTRATOR API VERIFICATION TESTS', 'cyan');
        this.log('═'.repeat(70), 'cyan');

        const backendReady = await this.waitForBackend();
        if (!backendReady) {
            this.results.failed++;
            this.results.errors.push({
                test: 'Readiness',
                error: `Orchestrator is not responding at ${this.baseUrl}`
            });
            return this.results;
        }

        this.log('1. Testing Health Endpoints\n', 'bright');
        await this.testEndpoint('Health Check', '/health', 200);
        await this.testEndpoint('API Health Alias', '/api/health', 200);
        const fullHealth = await this.testEndpoint('Full Health Check', '/health/full', 200, { timeoutMs: 20000 });
        await this.testEndpoint('Antigravity Status', '/antigravity/status', 200);

        this.log('\n2. Testing Response Headers\n', 'bright');
        this.results.total_tests++;
        const healthResponse = await this.makeRequest('/health');
        const contentType = healthResponse.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            this.log('  ✓ Content-Type is JSON', 'green');
            this.results.passed++;
        } else {
            this.log('  ✗ Content-Type is not JSON', 'red');
            this.results.failed++;
            this.results.errors.push({ test: 'Content-Type', error: `Unexpected content-type: ${contentType}` });
        }

        this.log('\n3. Testing Full Health Payload\n', 'bright');
        this.results.total_tests++;
        if (fullHealth && typeof fullHealth === 'object' && typeof fullHealth.overall_ok === 'boolean') {
            this.log('  ✓ Full health payload includes overall_ok', 'green');
            this.results.passed++;
        } else {
            this.log('  ✗ Full health payload shape is invalid', 'red');
            this.results.failed++;
            this.results.errors.push({ test: 'Full health payload', error: 'Missing overall_ok boolean' });
        }

        this.log('\n4. Testing Error Handling\n', 'bright');
        await this.testEndpoint('Non-existent endpoint (404)', '/definitely-missing-endpoint', 404);

        this.log('\n' + '─'.repeat(70), 'cyan');
        this.log('TEST SUMMARY', 'bright');
        this.log('─'.repeat(70), 'cyan');
        this.log(`Total Tests:  ${this.results.total_tests}`, 'cyan');
        this.log(`Passed:       ${this.results.passed}`, 'green');
        this.log(`Failed:       ${this.results.failed}`, this.results.failed > 0 ? 'red' : 'green');

        if (this.results.failed > 0) {
            this.log('\nFailed Tests:', 'red');
            for (const error of this.results.errors) {
                this.log(`  ✗ ${error.test}`, 'red');
                if (error.error) {
                    this.log(`    Error: ${error.error}`, 'yellow');
                } else {
                    this.log(`    Expected: ${error.expected}, Got: ${error.actual}`, 'yellow');
                }
            }
        }

        this.log('\n' + '═'.repeat(70) + '\n', 'cyan');
        return this.results;
    }
}

if (import.meta.url === `file://${process.argv[1]}`) {
    const test = new BackendVerificationTest();
    const results = await test.runTests();
    process.exit(results.failed > 0 ? 1 : 0);
}

export default BackendVerificationTest;
