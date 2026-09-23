import { registerPluginNamespace } from '@girder/core/pluginUtils';

// Imports for side effects: the "Filter Slides" folder button and the
// "Annotation Tools" entry in the top navigation.
import './views/HierarchyWidget';
import './views/GlobalNav';

import * as dsaCsv from './index';

registerPluginNamespace('dsa_csv', dsaCsv);
