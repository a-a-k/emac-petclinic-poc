package org.emac.poc;

import java.util.ArrayList;
import java.util.List;

import org.springframework.beans.BeansException;
import org.springframework.beans.factory.config.BeanDefinition;
import org.springframework.beans.factory.config.ConfigurableListableBeanFactory;
import org.springframework.beans.factory.support.BeanDefinitionRegistry;
import org.springframework.beans.factory.support.BeanDefinitionRegistryPostProcessor;
import org.springframework.context.ApplicationContextInitializer;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.core.env.Environment;
import org.springframework.objenesis.SpringObjenesis;
import org.springframework.util.ClassUtils;

/**
 * Prevents unrelated AWS demo integrations from performing eager data-plane
 * calls when the pinned Application Signals fork starts in the CI experiment.
 *
 * <p>The upstream source tree remains byte-for-byte unchanged. Only components
 * below a PetClinic {@code .aws.} package are replaced, without invoking their
 * constructors, after component scanning and before singleton instantiation.
 * None of those components participates in the declared owner-history journey.
 */
public final class AwsDemoNeutralizer
        implements ApplicationContextInitializer<ConfigurableApplicationContext> {

    private static final String ENABLED = "EMAC_NEUTRALIZE_AWS_DEMO_COMPONENTS";
    private static final String PETCLINIC_PREFIX = "org.springframework.samples.petclinic.";

    @Override
    public void initialize(ConfigurableApplicationContext applicationContext) {
        Environment environment = applicationContext.getEnvironment();
        if (!Boolean.parseBoolean(environment.getProperty(ENABLED, "false"))) {
            return;
        }

        ClassLoader classLoader = applicationContext.getClassLoader();
        applicationContext.addBeanFactoryPostProcessor(new BeanDefinitionRegistryPostProcessor() {
            @Override
            public void postProcessBeanDefinitionRegistry(BeanDefinitionRegistry registry)
                    throws BeansException {
                // Component scanning is completed by ConfigurationClassPostProcessor.
            }

            @Override
            public void postProcessBeanFactory(ConfigurableListableBeanFactory beanFactory)
                    throws BeansException {
                BeanDefinitionRegistry registry = (BeanDefinitionRegistry) beanFactory;
                List<String> candidates = new ArrayList<>();

                for (String beanName : beanFactory.getBeanDefinitionNames()) {
                    BeanDefinition definition = beanFactory.getBeanDefinition(beanName);
                    String className = definition.getBeanClassName();
                    if (className != null
                            && className.startsWith(PETCLINIC_PREFIX)
                            && className.contains(".aws.")) {
                        candidates.add(beanName);
                    }
                }

                SpringObjenesis objenesis = new SpringObjenesis();
                for (String beanName : candidates) {
                    BeanDefinition definition = beanFactory.getBeanDefinition(beanName);
                    String className = definition.getBeanClassName();
                    try {
                        Class<?> componentType = ClassUtils.forName(className, classLoader);
                        Object inertInstance = objenesis.newInstance(componentType);
                        registry.removeBeanDefinition(beanName);
                        beanFactory.registerSingleton(beanName, inertInstance);
                        System.out.println(
                                "EMAC_AWS_DEMO_NEUTRALIZED bean=" + beanName + " class=" + className);
                    } catch (ClassNotFoundException error) {
                        throw new IllegalStateException(
                                "Cannot neutralize AWS demo component " + className, error);
                    }
                }

                System.out.println("EMAC_AWS_DEMO_NEUTRALIZER count=" + candidates.size());
            }
        });
    }
}
