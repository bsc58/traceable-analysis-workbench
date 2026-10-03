import { defineConfig } from '@playwright/test';
export default defineConfig({testDir:'./e2e',testMatch:'*.spec.ts',workers:1,timeout:30000,reporter:[['list']],use:{baseURL:'http://127.0.0.1:8912',viewport:{width:1440,height:1050},headless:true},outputDir:'test-results'});
