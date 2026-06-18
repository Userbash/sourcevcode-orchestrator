#!/usr/bin/env node

/**
 * Master test runner for the current orchestrator repository.
 *
 * Test suites:
 * 1. Repository path checks
 * 2. Core build file checks
 * 3. Environment file sanity
 * 4. Host reverse proxy detection
 * 5. Compose validation
 * 6. Live orchestrator verification
 */

import { promises as fs } from 'fs';
import NginxDetectionTest from './system/nginx-detection.test.js';
import DockerComposeValidator from './system/docker-compose-validator.test.js';
import BackendVerificationTest from './system/backend-verification.test.js';

class MasterTestRunner {
    constructor() {
        this.projectRoot = process.cwd();
        this.results = {
            timestamp: new Date().toISOString(),
            total_suites: 0,
            passed_suites: 0,
            failed_suites: 0,
            test_results: []
        };
        this.colors = {
            reset: '\x1b[0m',
            bright: '\x1b[1m',
            green: '\x1b[32m',
            yellow: '\x1b[33m',
            red: '\x1b[31m',
            cyan: '\x1b[36m',
            magenta: '\x1b[35m'
        };
    }

    log(message, color = 'reset') {
        const c = this.colors[color] || '';
        console.log(`${c}${message}${this.colors.reset}`);
    }

    suiteSucceeded(result) {
        if (typeof result?.failed === 'number') {
            return result.failed === 0;
        }
        if (typeof result?.invalid === 'number') {
            return result.invalid === 0;
        }
        if (typeof result?.errors?.length === 'number') {
            return result.errors.length === 0 && result.valid !== false;
        }
        if (typeof result?.missing === 'number') {
            return result.missing === 0;
        }
        if (typeof result?.valid === 'boolean') {
            return result.valid;
        }
        return true;
    }

    async runTest(testName, testFunction) {
        this.results.total_suites++;
        this.log(`\n${'═'.repeat(70)}`, 'magenta');
        this.log(`  TEST SUITE: ${testName}`, 'magenta');
        this.log(`${'═'.repeat(70)}`, 'magenta');

        try {
            const result = await testFunction();
            const success = this.suiteSucceeded(result);

            if (success) {
                this.results.passed_suites++;
                this.log(`\n✓ ${testName} PASSED`, 'green');
            } else {
                this.results.failed_suites++;
                this.log(`\n✗ ${testName} FAILED`, 'red');
            }

            this.results.test_results.push({ suite: testName, success, result });
            return success;
        } catch (error) {
            this.results.failed_suites++;
            this.log(`\n✗ ${testName} ERROR: ${error.message}`, 'red');
            this.results.test_results.push({ suite: testName, success: false, error: error.message });
            return false;
        }
    }

    async verifyPaths() {
        this.log('\nVerifying project paths...', 'cyan');

        const pathsToCheck = [
            { path: `${this.projectRoot}/core`, type: 'dir', name: 'Core directory' },
            { path: `${this.projectRoot}/core/Dockerfile`, type: 'file', name: 'Core Dockerfile' },
            { path: `${this.projectRoot}/docker-compose.ai.yml`, type: 'file', name: 'AI compose file' },
            { path: `${this.projectRoot}/scripts/bootstrap_ai_stack.sh`, type: 'file', name: 'Bootstrap script' },
            { path: `${this.projectRoot}/tests/run-all-tests.js`, type: 'file', name: 'Master test runner' },
            { path: `${this.projectRoot}/.env`, type: 'file', name: '.env file' },
            { path: `${this.projectRoot}/.env.bridge`, type: 'file', name: '.env.bridge file' },
            { path: `${this.projectRoot}/.env.gemini.local`, type: 'file', name: '.env.gemini.local file' }
        ];

        const results = { total: 0, valid: 0, invalid: 0, errors: [] };
        for (const item of pathsToCheck) {
            results.total++;
            try {
                const stat = await fs.stat(item.path);
                const isValid = item.type === 'dir' ? stat.isDirectory() : stat.isFile();
                if (isValid) {
                    this.log(`  ✓ ${item.name}: ${item.path}`, 'green');
                    results.valid++;
                } else {
                    this.log(`  ✗ ${item.name}: exists but wrong type`, 'red');
                    results.invalid++;
                    results.errors.push(`${item.name} is not a ${item.type}`);
                }
            } catch {
                this.log(`  ✗ ${item.name}: NOT FOUND`, 'red');
                results.invalid++;
                results.errors.push(`${item.name} not found: ${item.path}`);
            }
        }
        return results;
    }

    async checkBuildFiles() {
        this.log('\nChecking build files...', 'cyan');

        const files = [
            { path: `${this.projectRoot}/core/Dockerfile`, name: 'Core Dockerfile', required: ['FROM', 'RUN', 'CMD'] },
            { path: `${this.projectRoot}/docker-compose.ai.yml`, name: 'AI compose file', required: ['services:', 'orchestrator:', 'rabbitmq:', 'db:'] }
        ];

        const results = { total: 0, valid: 0, invalid: 0, errors: [] };
        for (const file of files) {
            results.total++;
            try {
                const content = await fs.readFile(file.path, 'utf8');
                const missing = file.required.filter((marker) => !content.includes(marker));
                if (missing.length === 0) {
                    this.log(`  ✓ ${file.name}: required markers present`, 'green');
                    results.valid++;
                } else {
                    this.log(`  ✗ ${file.name}: missing markers ${missing.join(', ')}`, 'red');
                    results.invalid++;
                    results.errors.push(`${file.name} missing markers: ${missing.join(', ')}`);
                }
            } catch (error) {
                this.log(`  ✗ ${file.name}: ${error.message}`, 'red');
                results.invalid++;
                results.errors.push(`${file.name}: ${error.message}`);
            }
        }
        return results;
    }

    async checkEnvFiles() {
        this.log('\nVerifying environment files...', 'cyan');

        const envChecks = [
            { path: `${this.projectRoot}/.env`, vars: ['ORCHESTRATOR_PORT', 'AI_BRIDGE_DISABLE_SOURCECRAFT'] },
            { path: `${this.projectRoot}/.env.bridge`, vars: ['AI_BRIDGE_MESSAGE_BUS_BACKEND', 'AI_BRIDGE_MEMORY_ENABLED', 'AI_BRIDGE_AUTO_APPROVE'] },
            { path: `${this.projectRoot}/.env.gemini.local`, vars: ['AI_BRIDGE_OPENAI_AUTO_MODEL'] }
        ];

        const results = { total: 0, found: 0, missing: 0, errors: [] };
        for (const item of envChecks) {
            const content = await fs.readFile(item.path, 'utf8');
            const lines = content.split('\n');
            for (const varName of item.vars) {
                results.total++;
                const found = lines.some((line) => line.startsWith(`${varName}=`));
                if (found) {
                    this.log(`  ✓ ${varName}: configured in ${item.path.split('/').pop()}`, 'green');
                    results.found++;
                } else {
                    this.log(`  ✗ ${varName}: missing in ${item.path.split('/').pop()}`, 'red');
                    results.missing++;
                    results.errors.push(`${varName} missing in ${item.path}`);
                }
            }
        }
        return results;
    }

    async runFullTestSuite() {
        this.log('\n', 'magenta');
        this.log('╔════════════════════════════════════════════════════════════════╗', 'magenta');
        this.log('║                     FULL TEST SUITE STARTED                    ║', 'magenta');
        this.log('║         Running current orchestrator startup checks            ║', 'magenta');
        this.log('╚════════════════════════════════════════════════════════════════╝', 'magenta');

        await this.runTest('Path Verification', async () => this.verifyPaths());
        await this.runTest('Build Files Check', async () => this.checkBuildFiles());
        await this.runTest('Environment Files Check', async () => this.checkEnvFiles());
        await this.runTest('Reverse Proxy Detection', async () => new NginxDetectionTest().runTests());
        await this.runTest('Docker-Compose Validation', async () => new DockerComposeValidator().validateFile());
        await this.runTest('Live Orchestrator Verification', async () => new BackendVerificationTest().runTests());

        this.printSummary();
        await this.saveResults();
    }

    printSummary() {
        this.log('\n' + '═'.repeat(70), 'magenta');
        this.log('FINAL TEST SUMMARY', 'magenta');
        this.log('═'.repeat(70), 'magenta');
        this.log(`\nTotal Test Suites:  ${this.results.total_suites}`, 'cyan');
        this.log(`Passed:             ${this.results.passed_suites}`, 'green');
        this.log(`Failed:             ${this.results.failed_suites}`, this.results.failed_suites > 0 ? 'red' : 'green');

        if (this.results.failed_suites > 0) {
            this.log('\nSome test suites failed. Review the output above for details.', 'yellow');
        } else {
            this.log('\nAll test suites passed. The stack is ready for local operation.', 'green');
        }

        this.log('\n' + '═'.repeat(70) + '\n', 'magenta');
    }

    async saveResults() {
        try {
            const resultsFile = `${this.projectRoot}/tests/.test-results.json`;
            await fs.writeFile(resultsFile, JSON.stringify(this.results, null, 2));
            this.log(`Results saved to: ${resultsFile}`, 'cyan');
        } catch (error) {
            this.log(`Warning: Could not save results - ${error.message}`, 'yellow');
        }
    }
}

if (import.meta.url === `file://${process.argv[1]}`) {
    const runner = new MasterTestRunner();
    await runner.runFullTestSuite();
}

export default MasterTestRunner;
