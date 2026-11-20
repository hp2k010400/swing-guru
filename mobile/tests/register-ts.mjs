// Registers the extensionless-import hook before node:test loads the TypeScript sources.
import { register } from 'node:module';

register('./ts-resolve.mjs', import.meta.url);
