/**
 * Detects whether ports 80/443 are already occupied by an external reverse proxy.
 *
 * This is informational for the current orchestrator stack, which exposes its
 * own API on port 8000 and can optionally sit behind host-level Nginx.
 */

import { promises as fs } from 'fs';
import { join } from 'path';
import { spawn } from 'child_process';

class NginxDetectionTest {
    constructor() {
        this.projectRoot = process.cwd();
        this.ports = [80, 443];
        this.results = {
            nginx_found: false,
            external_nginx: false,
            ports_in_use: [],
            recommendations: [],
            config_file: 'docker-compose.ai.yml'
        };
        this.colors = {
            reset: '\x1b[0m',
            bright: '\x1b[1m',
            green: '\x1b[32m',
            yellow: '\x1b[33m',
            red: '\x1b[31m',
            cyan: '\x1b[36m'
        };
    }

    log(message, color = 'reset') {
        const c = this.colors[color] || '';
        console.log(`${c}${message}${this.colors.reset}`);
    }

    async checkPortWithSs(port) {
        return new Promise((resolve) => {
            const probe = spawn('ss', ['-tuln'], { stdio: 'pipe' });
            let output = '';

            probe.stdout.on('data', (data) => {
                output += data.toString();
            });

            probe.on('close', () => resolve(output.includes(`:${port}`)));
            probe.on('error', () => resolve(false));

            setTimeout(() => {
                probe.kill();
                resolve(false);
            }, 3000);
        });
    }

    async detectNginxProcess() {
        return new Promise((resolve) => {
            const ps = spawn('ps', ['aux'], { stdio: 'pipe' });
            let output = '';

            ps.stdout.on('data', (data) => {
                output += data.toString();
            });

            ps.on('close', () => resolve(output.includes('nginx')));
            ps.on('error', () => resolve(false));
        });
    }

    async runTests() {
        this.log('\n' + '═'.repeat(70), 'cyan');
        this.log('  HOST REVERSE PROXY DETECTION', 'cyan');
        this.log('═'.repeat(70) + '\n', 'cyan');

        this.log('1. Checking for running Nginx process...', 'bright');
        const nginxRunning = await this.detectNginxProcess();
        if (nginxRunning) {
            this.log('   ✓ Nginx process detected on system', 'green');
            this.results.nginx_found = true;
        } else {
            this.log('   ✗ No Nginx process detected', 'yellow');
        }

        this.log('\n2. Checking standard HTTP/HTTPS ports...', 'bright');
        for (const port of this.ports) {
            const portInUse = await this.checkPortWithSs(port);
            if (portInUse) {
                this.log(`   ✓ Port ${port} is in use`, 'yellow');
                this.results.ports_in_use.push(port);
                this.results.external_nginx = true;
            } else {
                this.log(`   ✗ Port ${port} is free`, 'green');
            }
        }

        this.log('\n3. Recommendations:', 'bright');
        if (this.results.external_nginx) {
            this.results.recommendations = [
                `External reverse proxy ports are in use: ${this.results.ports_in_use.join(', ')}`,
                'Keep the orchestrator stack on its native ports.',
                'If Nginx is used, proxy traffic to http://127.0.0.1:8000 for the API.',
                'RabbitMQ UI remains on http://127.0.0.1:15672 and Ollama on http://127.0.0.1:11434.'
            ];
        } else {
            this.results.recommendations = [
                'Ports 80/443 are free on the host.',
                'The orchestrator stack still uses docker-compose.ai.yml and its native ports by default.',
                'Expose http://127.0.0.1:8000 directly or place a reverse proxy in front later if needed.'
            ];
        }

        for (const rec of this.results.recommendations) {
            this.log(`   - ${rec}`, 'cyan');
        }

        await this.saveResults();
        this.log('\n' + '═'.repeat(70) + '\n', 'cyan');
        return this.results;
    }

    async saveResults() {
        const resultsFile = join(this.projectRoot, 'tests', '.nginx-detection-results.json');
        try {
            await fs.writeFile(resultsFile, JSON.stringify(this.results, null, 2));
            this.log(`Results saved to: ${resultsFile}`, 'cyan');
        } catch (error) {
            this.log(`Warning: Could not save results - ${error.message}`, 'yellow');
        }
    }
}

if (import.meta.url === `file://${process.argv[1]}`) {
    const test = new NginxDetectionTest();
    await test.runTests();
    process.exit(0);
}

export default NginxDetectionTest;
