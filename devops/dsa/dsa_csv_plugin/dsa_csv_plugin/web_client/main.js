import { registerPluginNamespace } from '@girder/core/pluginUtils';

// Import for side effects: injects the "Filter Slides" button into the folder view.
import './views/HierarchyWidget';

import * as dsaCsv from './index';

registerPluginNamespace('dsa_csv', dsaCsv);
