import type { UserConfig } from '@commitlint/types';

export default <UserConfig>{
  extends: ['@commitlint/config-conventional'],
  parserPreset: {
    parserOpts: {
      issuePrefixes: ['SF-'],
    },
  },
  rules: {
    'references-empty': [2, 'never'],
  },
};
