/**
 * Validates the compose file used by the current AI orchestrator stack.
 *
 * The validator is intentionally dependency-free so it can run on a clean host
 * Node installation without third-party packages.
 */

import { promises as fs } from 'fs';
import { resolve } from 'path';

class DockerComposeValidator {
    constructor(composePath = 'docker-compose.ai.yml') {
        this.composePath = composePath;
        this.projectRoot = process.cwd();
        this.results = {
            valid: true,
            warnings: [],
            errors: [],
            checks: {},
            services: []
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

    normalizeValue(raw = '') {
        return raw.replace(/^['"]|['"]$/g, '').trim();
    }

    parseCompose(content) {
        const config = { version: null, services: {}, volumes: {}, networks: {} };
        let section = null;
        let currentService = null;
        let currentSubsection = null;

        for (const rawLine of content.split('\n')) {
            const line = rawLine.replace(/\t/g, '    ');
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith('#')) {
                continue;
            }

            const indent = line.length - line.trimStart().length;

            if (indent === 0) {
                currentService = null;
                currentSubsection = null;
                if (trimmed.startsWith('version:')) {
                    config.version = this.normalizeValue(trimmed.split(':').slice(1).join(':'));
                } else if (trimmed.endsWith(':')) {
                    section = trimmed.slice(0, -1);
                }
                continue;
            }

            if (section === 'services') {
                if (indent === 2 && trimmed.endsWith(':')) {
                    currentService = trimmed.slice(0, -1);
                    currentSubsection = null;
                    config.services[currentService] = {
                        image: '',
                        build: null,
                        ports: [],
                        depends_on: [],
                        healthcheck: false
                    };
                    continue;
                }

                if (!currentService) {
                    continue;
                }

                const service = config.services[currentService];
                if (indent === 4) {
                    currentSubsection = null;
                    if (trimmed === 'build:') {
                        service.build = {};
                        currentSubsection = 'build';
                        continue;
                    }
                    if (trimmed === 'ports:') {
                        currentSubsection = 'ports';
                        continue;
                    }
                    if (trimmed === 'depends_on:') {
                        currentSubsection = 'depends_on';
                        continue;
                    }
                    if (trimmed === 'healthcheck:') {
                        service.healthcheck = true;
                        currentSubsection = 'healthcheck';
                        continue;
                    }

                    const [key, ...rest] = trimmed.split(':');
                    const value = this.normalizeValue(rest.join(':'));
                    if (key === 'image') {
                        service.image = value;
                    } else if (key === 'build' && value) {
                        service.build = { context: value };
                    }
                    continue;
                }

                if (indent === 6 && currentSubsection === 'build') {
                    const [key, ...rest] = trimmed.split(':');
                    const value = this.normalizeValue(rest.join(':'));
                    service.build ||= {};
                    service.build[key] = value;
                    continue;
                }

                if (indent === 6 && currentSubsection === 'ports' && trimmed.startsWith('- ')) {
                    service.ports.push(this.normalizeValue(trimmed.slice(2)));
                    continue;
                }

                if (indent === 6 && currentSubsection === 'depends_on') {
                    if (trimmed.startsWith('- ')) {
                        service.depends_on.push(this.normalizeValue(trimmed.slice(2)));
                    } else if (trimmed.endsWith(':')) {
                        service.depends_on.push(trimmed.slice(0, -1));
                    }
                }

                continue;
            }

            if ((section === 'volumes' || section === 'networks') && indent === 2 && trimmed.endsWith(':')) {
                config[section][trimmed.slice(0, -1)] = {};
            }
        }

        return config;
    }

    async validateFile() {
        this.log('\n' + '═'.repeat(70), 'cyan');
        this.log('  DOCKER COMPOSE CONFIGURATION VALIDATOR', 'cyan');
        this.log('═'.repeat(70) + '\n', 'cyan');

        try {
            const content = await fs.readFile(this.composePath, 'utf8');
            this.log(`1. Parsing ${this.composePath}...`, 'bright');
            const config = this.parseCompose(content);
            this.results.checks.yaml_syntax = true;
            this.log('   ✓ Compose file is readable', 'green');

            this.log('\n2. Validating compose structure...', 'bright');
            if (config.version) {
                this.log(`   ✓ Version: ${config.version}`, 'green');
                this.results.checks.version = true;
            } else {
                this.log('   ⚠ No explicit version found', 'yellow');
                this.results.warnings.push('Compose version is omitted');
            }

            const services = Object.keys(config.services || {});
            if (services.length > 0) {
                this.log(`   ✓ Services defined: ${services.join(', ')}`, 'green');
                this.results.checks.services = true;
                this.results.services = services;
            } else {
                this.log('   ✗ No services defined', 'red');
                this.results.valid = false;
                this.results.errors.push(`No services found in ${this.composePath}`);
            }

            this.log('\n3. Validating volumes...', 'bright');
            await this.validateVolumes(config);

            this.log('\n4. Validating service configurations...', 'bright');
            await this.validateServices(config);

            this.log('\n5. Validating build contexts...', 'bright');
            await this.validateBuildContexts(config);

            this.log('\n6. Validating networks...', 'bright');
            if (Object.keys(config.networks || {}).length > 0) {
                this.log(`   ✓ Networks defined: ${Object.keys(config.networks).join(', ')}`, 'green');
                this.results.checks.networks = true;
            } else {
                this.log('   ⚠ No custom networks defined', 'yellow');
                this.results.warnings.push('No custom networks defined');
            }
        } catch (error) {
            this.log(`   ✗ Error reading file: ${error.message}`, 'red');
            this.results.valid = false;
            this.results.errors.push(`File read error: ${error.message}`);
        }

        this.log('\n' + '─'.repeat(70), 'cyan');
        this.log('VALIDATION SUMMARY', 'bright');
        this.log('─'.repeat(70), 'cyan');

        if (this.results.valid) {
            this.log('✓ Compose configuration is VALID', 'green');
        } else {
            this.log('✗ Compose configuration has ERRORS', 'red');
        }

        for (const error of this.results.errors) {
            this.log(`  ✗ ${error}`, 'red');
        }
        for (const warning of this.results.warnings) {
            this.log(`  ⚠ ${warning}`, 'yellow');
        }

        this.log('\n' + '═'.repeat(70) + '\n', 'cyan');
        return this.results;
    }

    async validateVolumes(config) {
        const volumeNames = Object.keys(config.volumes || {});
        if (volumeNames.length === 0) {
            this.log('   ⚠ No root volumes defined', 'yellow');
            this.results.warnings.push('No root volumes defined');
            return;
        }

        this.log(`   ✓ Defined volumes: ${volumeNames.join(', ')}`, 'green');
        this.results.checks.volumes = true;
    }

    async validateServices(config) {
        for (const [serviceName, serviceConfig] of Object.entries(config.services || {})) {
            this.log(`\n   Service: ${serviceName}`, 'cyan');

            if (serviceConfig.image) {
                this.log(`     ✓ Image: ${serviceConfig.image}`, 'green');
            } else if (serviceConfig.build) {
                const context = serviceConfig.build.context || serviceConfig.build;
                this.log(`     ✓ Build context: ${context}`, 'green');
            } else {
                this.log('     ✗ No image or build definition', 'red');
                this.results.valid = false;
                this.results.errors.push(`Service ${serviceName} has no image or build definition`);
            }

            if (serviceConfig.healthcheck) {
                this.log('     ✓ Healthcheck configured', 'green');
            } else if (['orchestrator', 'rabbitmq'].includes(serviceName)) {
                this.log(`     ⚠ No healthcheck (recommended for ${serviceName})`, 'yellow');
                this.results.warnings.push(`No healthcheck for ${serviceName}`);
            }

            if (serviceConfig.ports.length > 0) {
                this.log(`     ✓ Ports: ${serviceConfig.ports.join(', ')}`, 'green');
            }

            if (serviceConfig.depends_on.length > 0) {
                this.log(`     ✓ Depends on: ${serviceConfig.depends_on.join(', ')}`, 'green');
            }
        }

        this.results.checks.services_detail = true;
    }

    async validateBuildContexts(config) {
        for (const [serviceName, serviceConfig] of Object.entries(config.services || {})) {
            if (!serviceConfig.build) {
                continue;
            }

            const build = typeof serviceConfig.build === 'string'
                ? { context: serviceConfig.build }
                : serviceConfig.build;
            const contextPath = resolve(this.projectRoot, build.context || '.');

            try {
                await fs.access(contextPath);
                this.log(`   ✓ ${serviceName}: ${build.context || '.'}`, 'green');
            } catch {
                this.log(`   ✗ ${serviceName}: context path not found - ${build.context || '.'}`, 'red');
                this.results.valid = false;
                this.results.errors.push(`Build context for ${serviceName} not found`);
            }

            if (build.dockerfile) {
                const dockerfilePath = resolve(contextPath, build.dockerfile);
                try {
                    await fs.access(dockerfilePath);
                    this.log(`     ✓ Dockerfile: ${build.dockerfile}`, 'green');
                } catch {
                    this.log(`     ✗ Dockerfile not found: ${build.dockerfile}`, 'red');
                    this.results.valid = false;
                    this.results.errors.push(`Dockerfile for ${serviceName} not found`);
                }
            }
        }

        this.results.checks.build_contexts = true;
    }
}

if (import.meta.url === `file://${process.argv[1]}`) {
    const validator = new DockerComposeValidator();
    const results = await validator.validateFile();
    process.exit(results.valid ? 0 : 1);
}

export default DockerComposeValidator;
